"""Grafo de LangGraph (Python) que orquesta un bucle de programación autónoma.

Flujo: preparar_worktree -> programador -> post_programador -> verificacion
       -> revisor (proponente) -> revisor_redteam -> agregar_revision
       -> (router) -> programador | compuerta_humana -> END

La verificación (cmake + pytest) es determinista y es la única fuente de verdad;
los agentes (opencode run) solo editan y dan feedback. El nido de revisión
(proponente + red-team + juez determinista) debate los hallazgos antes de la
compuerta humana, que usa interrupt() para pausar el grafo y pedir approve/reject.

Punto de entrada para `langgraph dev`: la variable `graph` al final del módulo.
El estado es un modelo Pydantic con valores por defecto, de modo que el grafo
acepta una entrada parcial (p. ej. solo {"task": "..."}) desde Studio o la API.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import List

from typing_extensions import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

# El CLI de LangGraph carga este módulo sin añadir `graph/` a sys.path;
# sin esto `from agent import run_agent` falla al arrancar `langgraph dev`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import run_agent

CMAKE_FLAGS = " ".join([
    "-DCMAKE_BUILD_TYPE=Release",
    "-DBUILD_ASAP=ON",
    "-DBUILD_EXECUTABLES=ON",
    "-DBUILD_IMAGEPROCESSING=ON",
    "-DBUILD_TESTS=OFF",
    "-DUSE_JPEG2000=OFF",
    "-DBUILD_MULTIRESOLUTIONIMAGEINTERFACE_DICOM_SUPPORT=OFF",
])

# Python usado para ejecutar pytest (la verificación). Por defecto el intérprete actual; el venv `.py311` no
# tenía pytest; anaconda sí (pytest, numpy, scikit-image). Sobrescríbelo con
# GRAPH_PYTHON si usas otro intérprete.
PYTHON = os.environ.get("GRAPH_PYTHON", sys.executable)

# ------------------------------------------------------------------ #
#  Tipos auxiliares                                                   #
# ------------------------------------------------------------------ #


class VerificationError(TypedDict):
    """Error de verificación estructurado (dict con estas claves)."""
    file: str
    line: int
    code: str
    message: str


class State(BaseModel):
    task: str = ""
    spec: str = ""
    diff: str = ""
    logs: str = ""
    iteration_count: int = 0
    veredicto: str = "pendiente"
    worktree_path: str = ""
    branch: str = ""
    passed: bool = False
    verification_errors: List[VerificationError] = Field(default_factory=list)
    verification_log: str = ""
    feedback: str = ""
    findings: List[dict] = Field(default_factory=list)
    veredictos: List[dict] = Field(default_factory=list)
    checks_objetivos: str = ""
    timings: dict = Field(default_factory=dict)
    plan: str = ""
    preflight_ok: bool = True
    preflight_reason: str = ""


# ------------------------------------------------------------------ #
#  Parseo determinista de errores de compilación / test              #
#  Convierte la salida cruda en VerificationError[] estructurados.   #
# ------------------------------------------------------------------ #

COMPILER_RE = re.compile(r"^(.+?):(\d+)(?::\d+)?:\s*(?:fatal\s+)?error(?:\s+([A-Za-z0-9]+))?\s*:\s*(.*)$")
TEST_RE = re.compile(r"^(.+?):(\d+):\s*(?:in\s+\S+|\w+(?:Error|Exception|Failure|Warning))(?::\s*(.*))?$")


def parse_verification_errors(output: str) -> List[VerificationError]:
    errors: List[VerificationError] = []
    for line in output.split("\n"):
        m = COMPILER_RE.match(line)
        if m:
            errors.append({
                "file": m.group(1),
                "line": int(m.group(2)),
                "code": m.group(3) or "",
                "message": (m.group(4) or "").strip(),
            })
            continue
        t = TEST_RE.match(line)
        if t:
            errors.append({
                "file": t.group(1),
                "line": int(t.group(2)),
                "code": "",
                "message": (t.group(3) or "").strip(),
            })
    return errors


# ------------------------------------------------------------------ #
#  Utilidad: ejecutar un comando capturando stdout+stderr             #
#  (equivalente a execSync con 2>&1). Lanza CalledProcessError con   #
#  `.output` si el comando falla o expira.                            #
# ------------------------------------------------------------------ #


def exec_capture(cmd: List[str], timeout: float, env: dict = None) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        out = e.output or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        raise subprocess.CalledProcessError(-1, cmd, output=out)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=out)
    return out


# ------------------------------------------------------------------ #
#  Utilidades del nido de revisión (proponente + red-team + juez)     #
# ------------------------------------------------------------------ #


def extract_json_array(text: str):
    """Extrae el primer array JSON de la salida libre de un agente.

    Los modelos locales a veces envuelven el JSON en markdown o añaden texto
    alrededor; esta función tolera eso y devuelve ``None`` si no parsea.
    """
    text = (text or "").strip()
    text = re.sub(r"```(?:json)?", "", text).strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


def objective_checks(worktree_path: str) -> str:
    """Chequeos deterministas (sin LLM) sobre el diff ya staged.

    - .pyc / __pycache__ en el stage (higiene)
    - git diff --cached --check (whitespace)
    - pyflakes sobre cada .py tocado (undefined name / import sin usar)

    Devuelve un resumen en texto plano que se incluye en el feedback final.
    """
    lines: List[str] = []
    try:
        changed = subprocess.run(
            ["git", "-C", worktree_path, "diff", "--cached", "--name-only", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
    except subprocess.CalledProcessError as err:
        changed = []
        lines.append(f"git diff --name-only falló: {err}")

    pycache = [f for f in changed if "__pycache__" in f or f.endswith((".pyc", ".pyo"))]
    lines.append(
        f"pycache en stage: {len(pycache)} archivo(s) -> {pycache}" if pycache
        else "pycache en stage: limpio"
    )

    try:
        # cr-at-eol: el upstream usa CRLF; sin esta opción git marca el CR como "trailing whitespace" (falso positivo).
        out = subprocess.run(
            ["git", "-C", worktree_path, "-c", "core.whitespace=blank-at-eol,cr-at-eol,space-before-tab",
             "diff", "--cached", "--check", "HEAD"],
            capture_output=True, text=True,
        ).stdout
        lines.append(f"git diff --check: {'OK' if not out.strip() else out.strip()}")
    except subprocess.CalledProcessError:
        lines.append("git diff --check: no disponible")

    py_files = [f for f in changed if f.endswith(".py") and "__pycache__" not in f]
    if py_files:
        for f in py_files:
            try:
                out = subprocess.run(
                    [PYTHON, "-m", "pyflakes", f"{worktree_path}/{f}"],
                    capture_output=True, text=True, timeout=60,
                ).stdout.strip()
                lines.append(f"pyflakes {f}: {'OK' if not out else out}")
            except Exception as err:  # noqa: BLE001 - no queremos que un lint rompa el grafo
                lines.append(f"pyflakes {f}: no disponible ({err})")
    else:
        lines.append("pyflakes: sin .py tocados")

    return "\n".join(lines)


# ------------------------------------------------------------------ #
#  Nodo 1 – preparar_worktree                                       #
#  Crea un git worktree y devuelve { worktree_path, branch }.       #
# ------------------------------------------------------------------ #


def preparar_worktree(state: State) -> dict:
    repo_root = os.getcwd()
    ts = int(time.time() * 1000)
    branch = f"feature/langgraph-{ts}"
    worktree_path = f"{repo_root}/.worktrees/langgraph-{ts}"

    subprocess.run(["git", "worktree", "add", worktree_path, "-b", branch], check=True)

    return {"worktree_path": worktree_path, "branch": branch}


# ------------------------------------------------------------------ #
#  Nodo 2 – verificacion (DETERMINISTA, sin LLM)                    #
#  Única fuente de verdad: ejecuta cmake --build y pytest.          #
#  Devuelve { passed, logs, verification_errors } estructurados.    #
# ------------------------------------------------------------------ #


def verificacion(state: State) -> dict:
    all_logs = ""
    fail_log = ""  # salida cruda de los pasos que fallan (lo que el agente necesita ver)
    ok = True
    errors: List[VerificationError] = []

    # Un modelo local puede "contestar" sin tocar nada (p. ej. escribir una llamada a herramienta como texto).
    if not state.diff.strip():
        msg = "Diff vacío: el programador no modificó ningún archivo."
        return {
            "logs": state.logs + f"=== verificacion ===\n{msg}\n",
            "passed": False,
            "verification_errors": [{
                "file": "",
                "line": 0,
                "code": "EMPTY_DIFF",
                "message": "El programador no modificó ningún archivo. Usa tus herramientas de edición, no escribas código como texto.",
            }],
            "verification_log": f"=== verificacion ===\n{msg}\n",
        }

    try:
        # Configurar (idempotente: si build/ ya existe, cmake solo reconfigura) y compilar.
        # Flags idénticas a las de AGENTS.md ("Compilar").
        wt = state.worktree_path
        output = exec_capture(["cmake", "-S", wt, "-B", f"{wt}/build", *CMAKE_FLAGS.split()], 300)
        output += exec_capture(["cmake", "--build", f"{wt}/build", f"-j{max(1, (os.cpu_count() or 1) // 2)}"], 600)
        all_logs += f"=== cmake output ===\n{output}\n\n"
    except subprocess.CalledProcessError as err:
        out = err.output or ""
        all_logs += f"=== cmake FAIL ===\n{out}\n\n"
        fail_log += f"=== cmake FAIL ===\n{out}\n\n"
        errors.extend(parse_verification_errors(out))
        ok = False

    try:
        pytest_env = {
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": f"{state.worktree_path}/scripts",
        }
        output = exec_capture(
            [PYTHON, "-m", "pytest", "-p", "no:cacheprovider", f"{state.worktree_path}/scripts/tests", "-q"],
            300,
            env=pytest_env,
        )
        all_logs += f"=== pytest output ===\n{output}\n\n"
    except subprocess.CalledProcessError as err:
        out = err.output or ""
        all_logs += f"=== pytest FAIL ===\n{out}\n\n"
        fail_log += f"=== pytest FAIL ===\n{out}\n\n"
        errors.extend(parse_verification_errors(out))
        ok = False

    return {
        "logs": state.logs + all_logs,
        "passed": ok,
        "verification_errors": errors,
        "verification_log": fail_log,
    }


# ------------------------------------------------------------------ #
#  Router condicional                                                #
#  - passed false && iteration_count <  3  -> programador (bucle)   #
#  - passed false && iteration_count >= 3  -> compuerta_humana      #
#    (escalado, no colgarse)                                        #
#  - passed true                           -> compuerta_humana      #
#    (revisión final)                                                #
# ------------------------------------------------------------------ #


def router(state: State) -> str:
    if not state.passed and state.iteration_count < 3:
        return "programador"
    return "compuerta_humana"


def preflight_router(state: State) -> str:
    # Si el preflight ve un problema evidente y aún hay iteraciones, vuelve al programador
    # (se salta la verificación cara). Acotado por iteration_count para no ciclar.
    if not state.preflight_ok and state.iteration_count < 3:
        return "programador"
    return "verificacion"


# ------------------------------------------------------------------ #
#  Nodo 4 – compuerta_humana                                        #
#  Pausa el grafo con interrupt() para revisión humana.             #
# ------------------------------------------------------------------ #


def compuerta_humana(state: State) -> dict:
    escalado = not state.passed and state.iteration_count >= 3

    answer = interrupt({
        "mensaje": "ESCALADO: se agotaron las iteraciones sin pasar la verificación. Revisa manualmente."
        if escalado
        else "Revisa el diff, los logs y el feedback del revisor para la aprobación final.",
        "escalado": escalado,
        "diff": state.diff,
        "logs": state.logs,
        "verification_errors": state.verification_errors,
        "feedback": state.feedback,
        "timings": state.timings,
    })

    veredicto = "aprobado" if answer == "approve" else ("rechazado" if answer == "reject" else "pendiente")
    return {"veredicto": veredicto}


# ------------------------------------------------------------------ #
#  Nodos agente (opencode run)                                       #
# ------------------------------------------------------------------ #

MODELS = {
    "programador": os.environ.get("GRAPH_MODEL_PROGRAMADOR", "local-ollama/gemma4-26b-64k"),
    "revisor": os.environ.get("GRAPH_MODEL_REVISOR", "local-ollama/gemma4-26b-64k"),
    "revisor_redteam": os.environ.get("GRAPH_MODEL_REVISOR_REDTEAM", "local-ollama/gemma4-26b-64k"),
    "planner": os.environ.get("GRAPH_MODEL_PLANNER", "local-ollama/gemma4-26b-64k"),
    "preflight": os.environ.get("GRAPH_MODEL_PREFLIGHT", "local-ollama/gemma4-26b-64k"),
}

REGLAS_PROGRAMADOR = (
    "Eres el programador del grafo. Trabajas DENTRO del directorio actual (un git worktree).\n"
    "- Edita los archivos directamente con tus herramientas; no hagas commit, push ni cambies de rama.\n"
    "- Haz solo lo que pide la tarea, nada más.\n"
    "- Corrige los errores de verificación reportados, si los hay.\n"
    "- Responde con un resumen breve de lo que cambiaste."
)

REGLAS_PLANNER = (
    "Eres el planner del grafo. Descompón la TAREA en pasos concretos y lista los archivos que probablemente "
    "haya que tocar. No edites archivos. Sé breve y accionable: un plan de 3-7 pasos.\n"
    "- NO incluyas pasos de compilar, buildear ni testear: el grafo los hace automáticamente.\n"
    "- NO incluyas pasos de commit ni push.\n"
    "- Céntrate SOLO en qué código cambiar y en qué archivos."
)

REGLAS_PREFLIGHT = (
    "Eres el preflight (triage) del grafo. Revisa el DIFF rápido y decide si procede verificarlo. "
    "No edites archivos. Responde SOLO con una palabra en la primera línea: OK o PROBLEMA, "
    "y en la segunda línea una razón breve.\n"
    "Marca PROBLEMA solo si hay algo evidente: diff vacío, scope creep (cambios fuera de la tarea), "
    "borrados accidentales de archivos, o cambios claramente erróneos."
)

REGLAS_PROPONENTE = (
    "Eres el revisor PROPOSITIVO de un nido de revisión (proponente + red-team + juez determinista). "
    "Revisa el diff y el resultado de la verificación y propón hallazgos accionables. No edites archivos.\n"
    "- Para cada hallazgo, VERIFICA en el archivo real (usa read/grep) y cita `archivo:línea` real, "
    "no las líneas del encabezado del diff (@@ ... @@).\n"
    "- Revisa en concreto: ¿el diff hace SOLO lo pedido? (scope creep) · ¿miembros redundantes que sombrean "
    "la clase base (p.ej. redeclarar _viewer)? · ¿includes/imports redundantes o faltantes? · ¿riesgos de "
    "memoria/señales (use-after-free, delete vs deleteLater, captura [this])? · ¿duplicación evitable?\n"
    "- Responde SOLO con un array JSON (sin markdown ni texto alrededor), con esta forma:\n"
    '[{"id": 1, "archivo": "ruta", "linea": 0, "afirmacion": "...", "evidencia": "...", "severidad": "alta|media|baja|info"}]\n'
    "- Si no hay hallazgos, responde exactamente: []"
)

REGLAS_REDTEAM = (
    "Eres el revisor RED-TEAM (desafiador) de un nido de revisión. Te dan los hallazgos de otro revisor; "
    "tu trabajo es confirmarlos o refutarlos contra el código real.\n"
    "- NO crees, edites ni borres NINGÚN archivo. Solo lee (read/grep) y responde.\n"
    "- Para cada hallazgo, abre el archivo citado (read/grep) y comprueba si la afirmación es cierta.\n"
    "- Marca cada hallazgo como: \"confirmado\" (cierto y relevante), \"refutado\" (falso) o \"dudoso\".\n"
    "- IMPORTANTE: responde ÚNICAMENTE con un array JSON válido, sin texto, markdown, encabezados ni "
    "explicaciones adicionales. Un solo bloque JSON, nada más:\n"
    '[{"id": <mismo id>, "veredicto": "confirmado|refutado|dudoso", "razon": "...", "evidencia": "..."}]\n'
    "- Si la lista de hallazgos está vacía, responde exactamente: []"
)


def planner(state: State) -> dict:
    prompt = f"{REGLAS_PLANNER}\n\nTAREA:\n{state.task}\n\nESPECIFICACIÓN:\n{state.spec}"
    try:
        out = run_agent(cwd=state.worktree_path, model=MODELS["planner"], prompt=prompt, timeout_ms=3 * 60_000)
        return {"plan": out, "logs": state.logs + f"=== planner ===\n{out}\n\n"}
    except Exception as err:
        return {"plan": "", "logs": state.logs + f"=== planner FALLÓ ===\n{err}\n\n"}


def programador(state: State) -> dict:
    prompt = (
        f"{REGLAS_PROGRAMADOR}\n\nTAREA:\n{state.task}\n\nESPECIFICACIÓN:\n{state.spec}\n\n"
        f"PLAN (del planner):\n{state.plan or '(sin plan)'}\n\n"
        f"ITERACIÓN: {state.iteration_count}\n\nFEEDBACK DEL REVISOR:\n{state.feedback}\n\n"
        f"FEEDBACK DEL PREFLIGHT:\n{state.preflight_reason or '(sin observaciones)'}\n\n"
        f"ERRORES DE VERIFICACIÓN (a corregir):\n{json.dumps(state.verification_errors, indent=2)}\n\n"
        f"OUTPUT CRUDO DE VERIFICACIÓN (por si los errores no parsean):\n{state.verification_log or '(sin fallos)'}"
    )
    try:
        out = run_agent(cwd=state.worktree_path, model=MODELS["programador"], prompt=prompt, timeout_ms=20 * 60_000)
        return {"logs": f"=== programador ===\n{out}\n\n"}
    except Exception as err:
        # Un fallo del modelo no debe matar el grafo: queda en los logs, verificacion verá el diff vacío y se reintenta o escala.
        return {"logs": f"=== programador FALLÓ ===\n{err}\n\n"}


# Incrementa el contador y calcula el diff REAL con git (el modelo no lo inventa).

TEXT_EXTS = {".cpp", ".h", ".hpp", ".c", ".cc", ".cxx", ".qrc", ".qss", ".py", ".txt", ".md", ".xml", ".json", ".cmake", ".ini", ".ui", ".pro"}


def strip_trailing_whitespace(worktree_path: str) -> None:
    """Quita trailing whitespace de los archivos de texto modificados (higiene determinista)."""
    status = subprocess.run(
        ["git", "-C", worktree_path, "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        if "D" in code:  # borrados: no existen, saltar
            continue
        path = line[3:].split(" -> ")[-1].strip().strip('"')
        if Path(path).suffix.lower() not in TEXT_EXTS:
            continue
        full = os.path.join(worktree_path, path)
        if not os.path.isfile(full):
            continue
        try:
            # newline="" preserva el \r (CRLF): solo quitamos espacios/tabs reales,
            # sin normalizar el estilo de fin de línea (el upstream usa CRLF).
            with open(full, "r", encoding="utf-8", errors="replace", newline="") as fh:
                lines = fh.readlines()
            stripped = [ln.rstrip(" \t") for ln in lines]
            if stripped != lines:
                with open(full, "w", encoding="utf-8", newline="") as fh:
                    fh.writelines(stripped)
        except Exception:
            pass


def post_programador(state: State) -> dict:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", state.worktree_path, *args],
            capture_output=True, text=True, check=True,
        ).stdout

    strip_trailing_whitespace(state.worktree_path)
    git("add", "-A")
    diff = git("diff", "--cached", "HEAD")
    return {"iteration_count": state.iteration_count + 1, "diff": diff}


def preflight(state: State) -> dict:
    if not state.diff.strip():
        return {
            "preflight_ok": False,
            "preflight_reason": "diff vacío: el programador no modificó archivos.",
            "logs": state.logs + "=== preflight: diff vacío ===\n\n",
        }
    prompt = f"{REGLAS_PREFLIGHT}\n\nTAREA:\n{state.task}\n\nDIFF:\n{state.diff[:8000]}"
    try:
        raw = run_agent(cwd=state.worktree_path, model=MODELS["preflight"], prompt=prompt, timeout_ms=3 * 60_000)
    except Exception as err:
        return {"preflight_ok": True, "preflight_reason": f"preflight no disponible: {err}"}

    # Fail-open: solo bloquea si la primera línea es claramente "PROBLEMA".
    ok = not raw.strip().upper().startswith("PROBLEMA")
    return {
        "preflight_ok": ok,
        "preflight_reason": raw.strip(),
        "logs": state.logs + f"=== preflight ===\n{raw}\n\n",
    }


def revisor(state: State) -> dict:
    prompt = (
        f"{REGLAS_PROPONENTE}\n\n"
        f"DIFF:\n{state.diff}\n\n"
        f"ERRORES DE VERIFICACIÓN:\n{json.dumps(state.verification_errors, indent=2)}\n\n"
        f"OUTPUT CRUDO DE VERIFICACIÓN:\n{state.verification_log or '(sin fallos)'}"
    )
    try:
        raw = run_agent(cwd=state.worktree_path, model=MODELS["revisor"], prompt=prompt, timeout_ms=10 * 60_000)
    except Exception as err:
        return {"logs": state.logs + f"=== revisor (proponente) FALLÓ ===\n{err}\n\n", "findings": []}

    findings = extract_json_array(raw)
    if findings is None:
        findings = [{
            "id": 0,
            "archivo": "",
            "linea": 0,
            "afirmacion": "la salida del proponente no parsea como JSON (revisar el log crudo)",
            "evidencia": raw,
            "severidad": "info",
        }]
    return {"logs": state.logs + f"=== revisor (proponente) ===\n{raw}\n\n", "findings": findings}


def revisor_redteam(state: State) -> dict:
    # Si el proponente no reportó nada, no hay nada que desafiar: ahorramos el agente.
    if not state.findings:
        return {"veredictos": [], "logs": state.logs + "=== revisor (red-team) omitido: sin hallazgos ===\n\n"}

    prompt = (
        f"{REGLAS_REDTEAM}\n\n"
        f"HALLAZGOS A VERIFICAR:\n{json.dumps(state.findings, indent=2)}\n\n"
        f"DIFF (contexto):\n{state.diff}"
    )
    try:
        # Sin timeout: el red-team (devstral 24B) verifica con read/grep y puede
        # tardar > 8 min en CPU; dejamos que termine en lugar de cortarlo.
        raw = run_agent(cwd=state.worktree_path, model=MODELS["revisor_redteam"], prompt=prompt, timeout_ms=None)
    except Exception as err:
        return {"logs": state.logs + f"=== revisor (red-team) FALLÓ ===\n{err}\n\n", "veredictos": []}

    veredictos = extract_json_array(raw)
    if veredictos is None:
        veredictos = [{
            "id": f.get("id", i),
            "veredicto": "dudoso",
            "razon": "red-team no devolvió JSON parseable",
            "evidencia": raw,
        } for i, f in enumerate(state.findings)]
    return {"logs": state.logs + f"=== revisor (red-team) ===\n{raw}\n\n", "veredictos": veredictos}


def agregar_revision(state: State) -> dict:
    """Junta hallazgos + veredictos + checks objetivos en un feedback consolidado."""
    checks = objective_checks(state.worktree_path)
    vered = {v.get("id"): v for v in state.veredictos}

    parts: List[str] = ["=== Revisión deliberativa (proponente + red-team + juez determinista) ===\n"]

    if state.findings:
        parts.append("-- Hallazgos del proponente --")
        for f in state.findings:
            v = vered.get(f.get("id"))
            tag = f"[{v.get('veredicto', 'sin veredicto')}]" if v else "[sin veredicto]"
            sev = f.get("severidad", "?").upper()
            partes = [tag, sev, f"{f.get('archivo')}:{f.get('linea')}", "—", str(f.get("afirmacion"))]
            parts.append(" ".join(p for p in partes if p))
            if v and v.get("razon"):
                parts.append(f"    red-team: {v['razon']}")
            if f.get("evidencia"):
                parts.append(f"    evidencia: {f['evidencia']}")

        disc = [
            f for f in state.findings
            if f.get("severidad") in ("alta", "media")
            and vered.get(f.get("id"), {}).get("veredicto") != "confirmado"
        ]
        if disc:
            parts.append("\n-- Discrepancias sin resolver (revisa tú) --")
            for f in disc:
                parts.append(f"  #{f.get('id')} {f.get('archivo')}:{f.get('linea')} — {f.get('afirmacion')}")
    else:
        parts.append("-- Sin hallazgos del proponente --")

    parts.append("\n-- Checks objetivos (determinista, sin LLM) --")
    parts.append(checks)

    return {"feedback": "\n".join(parts), "checks_objetivos": checks}


# ------------------------------------------------------------------ #
#  Construcción del grafo                                            #
# ------------------------------------------------------------------ #


def _timed(fn):
    """Envuelve un nodo para registrar su duración (segundos) en state['timings']."""
    name = fn.__name__

    def wrapper(state: State) -> dict:
        start = time.time()
        result = fn(state) or {}
        elapsed = round(time.time() - start, 2)
        out = dict(result)
        out["timings"] = {**state.timings, name: elapsed}
        return out

    wrapper.__name__ = name
    return wrapper


def build_graph(checkpointer):
    return (
        StateGraph(State)
        .add_node("preparar_worktree", _timed(preparar_worktree))
        .add_node("planner", _timed(planner))
        .add_node("programador", _timed(programador))
        .add_node("post_programador", _timed(post_programador))
        .add_node("preflight", _timed(preflight))
        .add_node("verificacion", _timed(verificacion))
        .add_node("revisor", _timed(revisor))
        .add_node("revisor_redteam", _timed(revisor_redteam))
        .add_node("agregar_revision", _timed(agregar_revision))
        .add_node("compuerta_humana", _timed(compuerta_humana))
        .add_edge(START, "preparar_worktree")
        .add_edge("preparar_worktree", "planner")
        .add_edge("planner", "programador")
        .add_edge("programador", "post_programador")
        .add_edge("post_programador", "preflight")
        .add_conditional_edges("preflight", preflight_router, ["programador", "verificacion"])
        .add_edge("verificacion", "revisor")
        .add_edge("revisor", "revisor_redteam")
        .add_edge("revisor_redteam", "agregar_revision")
        .add_conditional_edges("agregar_revision", router, ["programador", "compuerta_humana"])
        .add_edge("compuerta_humana", END)
        .compile(checkpointer=checkpointer)
    )


def initial_state(task: str, spec: str = "") -> State:
    return State(task=task, spec=spec)


# Punto de entrada para LangGraph Studio / `langgraph dev`.
# Sin checkpointer propio: el servidor de LangGraph aporta el suyo (necesario para interrupt()).
graph = build_graph(None)
