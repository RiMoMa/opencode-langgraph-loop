"""Ejecuta un agente de OpenCode (`opencode run`) en `cwd` y devuelve su salida de texto.

--auto aprueba lo que no esté denegado en la sección `permission` de opencode.json:
esas reglas de denegación son la capa de guardrails. Sin shell: el prompt va como un único argumento.
"""

import os
import subprocess


def _text(data):
    """Devuelve bytes/str como str (bytes decodificados), o '' si es None."""
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode(errors="replace")
    return data


def run_agent(*, cwd: str, model: str, prompt: str, timeout_ms: int = None, agent: str = None) -> str:
    args = ["opencode", "run", "--auto", "--model", model]
    if agent:
        args += ["--agent", agent]
    args += ["--title", "langgraph", "--archived", prompt]

    # opencode toma el directorio de $PWD, no del cwd real: sin esto edita el repo desde el que se lanzó el grafo.
    env = {**os.environ, "PWD": cwd}

    # timeout_ms=None significa "sin límite" (el agente corre hasta terminar).
    timeout = None if timeout_ms is None else timeout_ms / 1000
    try:
        proc = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as err:
        # Incluir la salida parcial para saber qué estaba haciendo el modelo antes de colgarse.
        detail = f"opencode run excedió {timeout_ms} ms"
        stdout_partial = _text(err.stdout).strip()
        stderr_partial = _text(err.stderr).strip()
        if stdout_partial:
            detail += f"\n--- stdout parcial ---\n{stdout_partial}"
        if stderr_partial:
            detail += f"\n--- stderr parcial ---\n{stderr_partial}"
        raise RuntimeError(detail)
    except OSError as err:
        raise RuntimeError(str(err))

    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        raise RuntimeError(f"opencode run salió con {proc.returncode}: {err}")

    return (proc.stdout or "").strip()
