#!/usr/bin/env python3
"""
LoRA fine-tuning subprocess for distil-whisper/distil-large-v3.
4GB GPU config: r=8, alpha=16, batch=1, grad_accumulation=8, FP16, gradient checkpointing.
Main sidecar unloads LLM+Turbo before launching this subprocess to free VRAM.
"""
import argparse
import gc
import json
import os
import shutil
import subprocess
import sys

import numpy as np
import torch
from torch.utils.data import DataLoader, Dataset
from peft import LoraConfig, get_peft_model
from transformers import WhisperForConditionalGeneration, WhisperProcessor

HF_MODEL_ID = "distil-whisper/distil-large-v3"
SAMPLE_RATE = 16000


class ASRDataset(Dataset):
    def __init__(self, samples: list, processor: WhisperProcessor):
        self.samples = samples
        self.processor = processor

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        s = self.samples[idx]
        audio = np.load(s["audio_path"]).astype(np.float32)
        inputs = self.processor(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt")
        labels = self.processor.tokenizer(
            s["transcript"], return_tensors="pt", padding=False
        ).input_ids
        return {
            "input_features": inputs.input_features.squeeze(0),
            "labels": labels.squeeze(0),
        }


def collate_fn(batch):
    input_features = torch.stack([b["input_features"] for b in batch])
    label_list = [b["labels"] for b in batch]
    max_len = max(l.size(0) for l in label_list)
    labels_padded = torch.full((len(label_list), max_len), -100, dtype=torch.long)
    for i, l in enumerate(label_list):
        labels_padded[i, : l.size(0)] = l
    return {"input_features": input_features, "labels": labels_padded}


def write_progress(path, status, progress, epoch, total_epochs, samples, error=None):
    with open(path, "w") as f:
        json.dump(
            {
                "status": status,
                "progress": progress,
                "epoch": epoch,
                "total_epochs": total_epochs,
                "samples": samples,
                "error": error,
            },
            f,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    progress_path = os.path.join(args.data_dir, "finetune_progress.json")
    clips_dir = os.path.join(args.data_dir, "audio_clips")
    log_path = os.path.join(args.data_dir, "passive_log.jsonl")

    # Load training samples (only entries that have a valid audio file)
    samples = []
    with open(log_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "audio_file" not in entry:
                continue
            audio_path = os.path.join(clips_dir, entry["audio_file"])
            if not os.path.isfile(audio_path):
                continue
            samples.append({"audio_path": audio_path, "transcript": entry["user_edited"]})

    if len(samples) < 50:
        write_progress(
            progress_path, "error", 0.0, 0, args.epochs, len(samples),
            f"Not enough samples: {len(samples)} < 50",
        )
        sys.exit(1)

    write_progress(progress_path, "running", 0.0, 0, args.epochs, len(samples))
    print(f"Training on {len(samples)} samples, {args.epochs} epoch(s), lr={args.lr}")

    # Load HF model (downloads distil-large-v3 HF format if not cached)
    print(f"Loading {HF_MODEL_ID} from cache or downloading...")
    processor = WhisperProcessor.from_pretrained(HF_MODEL_ID, cache_dir=args.model_dir)
    model = WhisperForConditionalGeneration.from_pretrained(
        HF_MODEL_ID,
        torch_dtype=torch.float16,
        cache_dir=args.model_dir,
    )
    model.gradient_checkpointing_enable()
    model = model.cuda()

    # Apply LoRA (4GB GPU: r=8, alpha=16)
    # No task_type: SEQ_2_SEQ_LM injects input_ids but Whisper uses input_features
    lora_cfg = LoraConfig(
        r=8,
        lora_alpha=16,
        target_modules=["q_proj", "v_proj"],
        lora_dropout=0.05,
        bias="none",
    )
    model = get_peft_model(model, lora_cfg)
    model.print_trainable_parameters()

    dataset = ASRDataset(samples, processor)
    loader = DataLoader(dataset, batch_size=1, shuffle=True, collate_fn=collate_fn)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)

    GRAD_ACCUM = 8
    global_step = 0

    for epoch in range(1, args.epochs + 1):
        model.train()
        optimizer.zero_grad()
        for step, batch in enumerate(loader, 1):
            input_features = batch["input_features"].to("cuda", dtype=torch.float16)
            labels = batch["labels"].to("cuda")

            outputs = model(input_features=input_features, labels=labels)
            loss = outputs.loss / GRAD_ACCUM
            loss.backward()

            if step % GRAD_ACCUM == 0 or step == len(loader):
                optimizer.step()
                optimizer.zero_grad()
                global_step += 1

                if global_step % 10 == 0:
                    progress = (epoch - 1 + step / len(loader)) / args.epochs
                    write_progress(
                        progress_path, "running", round(progress, 4),
                        epoch, args.epochs, len(samples),
                    )
                    print(f"epoch {epoch} step {step}/{len(loader)} loss={loss.item() * GRAD_ACCUM:.4f}")

        write_progress(
            progress_path, "running", round(epoch / args.epochs, 4),
            epoch, args.epochs, len(samples),
        )
        print(f"Epoch {epoch}/{args.epochs} complete")

    # Merge LoRA into base weights
    merged_dir = os.path.join(args.output_dir, "lora-merged")
    print("Merging LoRA weights...")
    merged = model.merge_and_unload()
    merged.save_pretrained(merged_dir)
    processor.save_pretrained(merged_dir)
    del merged, model
    gc.collect()
    torch.cuda.empty_cache()

    # Convert merged HF model → CT2 INT8 (best-effort: ctranslate2/transformers version may not match)
    ct2_out = os.path.join(args.model_dir, "distil-lora-ct2")
    ct2_exe = os.path.join(os.path.dirname(sys.executable), "ct2-transformers-converter.exe")
    print(f"Converting to CT2 INT8 -> {ct2_out}")
    try:
        subprocess.run(
            [ct2_exe, "--model", merged_dir, "--output_dir", ct2_out,
             "--quantization", "int8", "--force"],
            check=True,
        )
        # Copy preprocessor_config.json — required by faster-whisper
        src = os.path.join(merged_dir, "preprocessor_config.json")
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(ct2_out, "preprocessor_config.json"))
        print("CT2 conversion complete.")
    except Exception as e:
        # Training is complete even if CT2 conversion fails; merged model is at merged_dir
        print(f"WARNING: CT2 conversion failed: {e}. Merged model at: {merged_dir}", file=sys.stderr)

    write_progress(progress_path, "complete", 1.0, args.epochs, args.epochs, len(samples))
    print("Training complete.")


if __name__ == "__main__":
    main()
