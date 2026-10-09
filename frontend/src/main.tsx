import { createRoot } from "react-dom/client";
import { RestBackend } from "@/infrastructure/rest";
import { App } from "@/presentation/App";
import "@/presentation/style.css";

createRoot(document.getElementById("root")!).render(<App backend={new RestBackend(window.__MEMES_CONFIG__)} />);
