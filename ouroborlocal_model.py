# ouroboros/local_model.py
from __future__ import annotations
import logging, os, signal, subprocess, sys, time
from typing import Optional
import requests
from huggingface_hub import hf_hub_download

log = logging.getLogger(__name__)

class LocalModelManager:
    def __init__(self):
        self._proc: Optional[subprocess.Popen] = None
        self._port = 8000

    def download_model(self, repo_id: str, filename: str) -> str:
        log.info(f"📥 Скачиваю {repo_id}/{filename} (\~21 ГБ, 10–30 мин)...")
        return hf_hub_download(repo_id=repo_id, filename=filename, resume_download=True)

    def start_server(self, model_path: str):
        if self._proc and self._proc.poll() is None:
            return

        n_gpu_layers = int(os.getenv("N_GPU_LAYERS", "40"))   # 35–45 для T4
        n_ctx = int(os.getenv("LOCAL_CTX", "8192"))

        cmd = [
            sys.executable, "-m", "llama_cpp.server",
            "--model", model_path,
            "--port", str(self._port),
            "--n_gpu_layers", str(n_gpu_layers),
            "--n_ctx", str(n_ctx),
            "--chat_format", "qwen",          # специально для Qwen3.5
            "--host", "0.0.0.0",
            "--n_batch", "512",
            "--n_threads", "8",
            "--verbose", "false"
        ]

        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      start_new_session=True)
        log.info(f"🚀 Запускаю llama-server (n_gpu_layers={n_gpu_layers}, ctx={n_ctx})...")

        for _ in range(90):  # до 7.5 минут
            try:
                r = requests.get(f"http://127.0.0.1:{self._port}/v1/models", timeout=5)
                if r.status_code == 200:
                    log.info("✅ Local Qwen3.5-35B-A3B сервер готов!")
                    return
            except:
                time.sleep(5)
        raise RuntimeError("❌ Сервер не запустился")

    def stop(self):
        if self._proc:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            self._proc = None

get_local_manager = LocalModelManager
