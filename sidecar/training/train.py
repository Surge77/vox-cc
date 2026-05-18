#!/usr/bin/env python3
"""
LoRA fine-tuning subprocess for distil-whisper/distil-large-v3.
4GB GPU config: r=8, alpha=16, batch=1, grad_accumulation=8, FP16, gradient checkpointing.
Invoked by finetuning.py via asyncio.create_subprocess_exec — not imported directly.
All heavy imports are deferred inside main() to keep module-level import fast.
"""
import argparse
import json
import os
import sys

# Constants live at module level (no heavy deps)
HF_MODEL_ID = "distil-whisper/distil-large-v3"
SAMPLE_RATE = 16000
GRAD_ACCUM = 8
LORA_R = 8
LORA_ALPHA = 16
LORA_DROPOUT = 0.05


# ---------------------------------------------------------------------------
# Progress helpers — write + fsync to avoid partial writes on crash
# ---------------------------------------------------------------------------

def _write_progress(
    path: str,
    status: str,
    progress: float,
    epoch: int,
    total_epochs: int,
    samples: int,
    error: str | None = None,
) -> None:
    payload = json.dumps({
        "status": status,
        "progress": progress,
        "epoch": epoch,
        "total_epochs": total_epochs,
        "samples": samples,
        "error": error,
    })
    with open(path, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# Dataset (defined at module level to avoid redefining inside main each call)
# Dataset class itself has no heavy imports — only uses numpy which is already
# a transitive dep of torch; it is only instantiated after torch is imported.
# ---------------------------------------------------------------------------

def _make_dataset_and_loader(samples: list, processor, batch_size: int):
    """Build ASRDataset and DataLoader. Called after torch/transformers are imported."""
    import numpy as np
    import torch
    from torch.utils.data import DataLoader, Dataset

    class ASRDataset(Dataset):
        def __init__(self, items: list, proc) -> None:
            self.items = items
            self.proc = proc

        def __len__(self) -> int:
            return len(self.items)

        def __getitem__(self, idx: int) -> dict:
            s = self.items[idx]
            audio = np.load(s["audio_path"]).astype(np.float32)
            # input_features: always 80x3000 fixed shape — no padding needed
            inputs = self.proc(audio, sampling_rate=SAMPLE_RATE, return_tensors="pt")
            labels = self.proc.tokenizer(
                s["transcript"], return_tensors="pt", padding=False
            ).input_ids
            return {
                "input_features": inputs.input_features.squeeze(0),
                "labels": labels.squeeze(0),
            }

    def collate_fn(batch: list) -> dict:
        input_features = torch.stack([b["input_features"] for b in batch])
        label_list = [b["labels"] for b in batch]
        max_len = max(lbl.size(0) for lbl in label_list)
        padded = torch.full((len(label_list), max_len), -100, dtype=torch.long)
        for i, lbl in enumerate(label_list):
            padded[i, : lbl.size(0)] = lbl
        return {"input_features": input_features, "labels": padded}

    dataset = ASRDataset(samples, processor)
    loader = DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=True,
        collate_fn=collate_fn,
        num_workers=0,  # Windows: multiprocessing with CUDA is fragile
        pin_memory=False,
    )
    return loader


# ---------------------------------------------------------------------------
# Sample loading
# ---------------------------------------------------------------------------

def _load_samples(data_dir: str) -> list:
    """
    Parse passive_log.jsonl and return valid training samples.
    Valid = has audio_file field AND the .npy file exists on disk.
    Uses user_edited as ground truth; falls back to raw_asr if user_edited is empty.
    """
    log_path = os.path.join(data_dir, "passive_log.jsonl")
    clips_dir = os.path.join(data_dir, "audio_clips")
    samples = []
    try:
        with open(log_path, encoding="utf-8") as f:
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
                transcript = entry.get("user_edited", "").strip()
                if not transcript:
                    transcript = entry.get("raw_asr", "").strip()
                if not transcript:
                    continue
                samples.append({"audio_path": audio_path, "transcript": transcript})
    except FileNotFoundError:
        pass
    return samples


# ---------------------------------------------------------------------------
# CT2 conversion
# ---------------------------------------------------------------------------

def _convert_to_ct2(merged_dir: str, ct2_out: str) -> None:
    """
    Run ct2-transformers-converter on the merged HF model directory.
    Raises subprocess.CalledProcessError on failure.
    """
    import shutil
    import subprocess

    converter = shutil.which("ct2-transformers-converter")
    if converter is None:
        # Fallback: look next to sys.executable (inside the venv Scripts/ directory)
        scripts_dir = os.path.dirname(sys.executable)
        candidate = os.path.join(scripts_dir, "ct2-transformers-converter.exe")
        if os.path.isfile(candidate):
            converter = candidate
        else:
            raise FileNotFoundError(
                "ct2-transformers-converter not found on PATH or next to Python executable"
            )

    subprocess.run(
        [
            converter,
            "--model", merged_dir,
            "--output_dir", ct2_out,
            "--quantization", "int8",
            "--force",
        ],
        check=True,
    )

    # Copy preprocessor_config.json — required by faster-whisper at inference time
    src = os.path.join(merged_dir, "preprocessor_config.json")
    dst = os.path.join(ct2_out, "preprocessor_config.json")
    if os.path.isfile(src) and not os.path.isfile(dst):
        shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Main training entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="LoRA fine-tune distil-whisper/distil-large-v3")
    parser.add_argument("--data-dir", required=True, help="Path to ~/.vox/data/")
    parser.add_argument("--model-dir", required=True, help="Path to sidecar/models/")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--output-dir", required=True, help="Scratch dir for merge output")
    args = parser.parse_args()

    progress_path = os.path.join(args.data_dir, "finetune_progress.json")

    try:
        _run_training(args, progress_path)
    except Exception as exc:
        _write_progress(
            progress_path, "error", 0.0, 0, args.epochs, 0, str(exc)
        )
        print(f"FATAL: {exc}", file=sys.stderr)
        sys.exit(1)


def _run_training(args, progress_path: str) -> None:
    """Inner function so the outer main() can catch all exceptions cleanly."""
    import gc
    import torch
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    from peft import LoraConfig, TaskType, get_peft_model

    # --- Load samples ---
    samples = _load_samples(args.data_dir)
    if len(samples) < 50:
        raise RuntimeError(
            f"Not enough valid training samples: {len(samples)} (minimum 50 required)"
        )

    _write_progress(
        progress_path, "running", 0.0, 0, args.epochs, len(samples)
    )
    print(f"[train] {len(samples)} samples, {args.epochs} epoch(s), lr={args.lr}", flush=True)

    # --- Load HF model and processor ---
    print(f"[train] Loading {HF_MODEL_ID} (HF format, cache={args.model_dir})", flush=True)
    processor = WhisperProcessor.from_pretrained(HF_MODEL_ID, cache_dir=args.model_dir)
    model = WhisperForConditionalGeneration.from_pretrained(
        HF_MODEL_ID,
        torch_dtype=torch.float16,
        cache_dir=args.model_dir,
    )

    # Gradient checkpointing before PEFT wrap — saves ~30% activation memory on 4GB
    model.gradient_checkpointing_enable()
    model = model.cuda()

    # --- Apply LoRA ---
    lora_cfg = LoraConfig(
        r=LORA_R,
        lora_alpha=LORA_ALPHA,
        target_modules=["q_proj", "v_proj"],
        lora_dropout=LORA_DROPOUT,
        bias="none",
        task_type=TaskType.SEQ_2_SEQ_LM,
    )
    model = get_peft_model(model, lora_cfg)
    model.print_trainable_parameters()

    # --- DataLoader ---
    loader = _make_dataset_and_loader(samples, processor, batch_size=1)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    total_steps = len(loader) * args.epochs
    global_step = 0

    # --- Training loop ---
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

                # Progress update every 10 optimizer steps
                if global_step % 10 == 0:
                    frac = ((epoch - 1) * len(loader) + step) / (total_steps)
                    _write_progress(
                        progress_path, "running", round(frac, 4),
                        epoch, args.epochs, len(samples),
                    )
                    print(
                        f"[train] epoch {epoch}/{args.epochs} "
                        f"step {step}/{len(loader)} "
                        f"loss={loss.item() * GRAD_ACCUM:.4f}",
                        flush=True,
                    )

        epoch_progress = round(epoch / args.epochs, 4)
        _write_progress(
            progress_path, "running", epoch_progress,
            epoch, args.epochs, len(samples),
        )
        print(f"[train] Epoch {epoch}/{args.epochs} complete", flush=True)

    # --- Merge LoRA into base weights ---
    merged_dir = os.path.join(args.output_dir, "lora-merged")
    print(f"[train] Merging LoRA weights -> {merged_dir}", flush=True)
    merged_model = model.merge_and_unload()
    merged_model.save_pretrained(merged_dir)
    processor.save_pretrained(merged_dir)
    del merged_model, model
    gc.collect()
    torch.cuda.empty_cache()

    # --- Convert to CTranslate2 INT8 ---
    ct2_out = os.path.join(args.model_dir, "distil-lora-ct2")
    print(f"[train] Converting merged model to CT2 INT8 -> {ct2_out}", flush=True)
    try:
        _convert_to_ct2(merged_dir, ct2_out)
        print("[train] CT2 conversion complete.", flush=True)
    except Exception as conv_err:
        # Training succeeded; CT2 conversion failure is non-fatal — merged model usable
        print(
            f"[train] WARNING: CT2 conversion failed: {conv_err}. "
            f"Merged HF model available at: {merged_dir}",
            file=sys.stderr,
            flush=True,
        )

    _write_progress(
        progress_path, "complete", 1.0,
        args.epochs, args.epochs, len(samples),
    )
    print("[train] Training complete.", flush=True)


if __name__ == "__main__":
    main()
