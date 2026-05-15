import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import App from "./App";
import Settings from "./Settings";
import FineTuningDashboard from "./FineTuningDashboard";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <BrowserRouter>
    <Routes>
      <Route path="/" element={<App />} />
      <Route path="/settings" element={<Settings />} />
      <Route path="/finetune" element={<FineTuningDashboard />} />
    </Routes>
  </BrowserRouter>
);
