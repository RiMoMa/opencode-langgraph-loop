"""Ejecutor del grafo por línea de comandos (equivalente a run.ts).

Uso:
  python3 graph/run.py [--spec "<contexto>"] "<tarea>"   nueva ejecución
  python3 graph/run.py --resume <thread>                 reanudar una pausada

GRAPH_DB=:memory: usa un checkpointer en memoria (solo pruebas).
GRAPH_DB apunta a una base sqlite (por defecto ./checkpoints.db) si está
instalado `langgraph-checkpoint-sqlite`; si no, se cae a memoria con aviso.
"""

import json
import os
import sys
import time

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from workflow import build_graph, initial_state


def _run(checkpointer, *, thread_id: str, task: str, spec: str, resume: bool) -> None:
    graph = build_graph(checkpointer)
    config = {"configurable": {"thread_id": thread_id}}

    state_input = None if resume else initial_state(task, spec)

    while True:
        for update in graph.stream(state_input, {**config, "stream_mode": "updates"}):
            for node in update:
                if not node.startswith("__"):
                    print(f"✔ {node}")

        snap = graph.get_state(config)
        pending = [i for t in snap.tasks for i in (t.interrupts or [])]
        if not pending:
            break

        info = pending[0].value
        print(f"\n{info.get('mensaje')}{'  [ESCALADO]' if info.get('escalado') else ''}")
        print(f"--- diff ---\n{info.get('diff') or '(vacío)'}")
        if info.get("verification_errors"):
            print("--- errores ---\n" + json.dumps(info["verification_errors"], indent=2))
        print(f"--- logs ---\n{info.get('logs') or '(vacío)'}")
        if info.get("feedback"):
            print(f"--- feedback del revisor ---\n{info['feedback']}")

        answer = input("\n¿approve / reject? (otro = pausar) ").strip()
        if answer not in ("approve", "reject"):
            print(f"Pausado. Reanuda con: --resume {thread_id}")
            sys.exit(0)
        state_input = Command(resume=answer)

    final = graph.get_state(config).values
    print(f"\nveredicto={final.get('veredicto')} rama={final.get('branch')} worktree={final.get('worktree_path')}")


def main() -> None:
    args = sys.argv[1:]
    resume_idx = args.index("--resume") if "--resume" in args else -1
    spec_idx = args.index("--spec") if "--spec" in args else -1

    resume = resume_idx >= 0
    thread_id = args[resume_idx + 1] if resume_idx >= 0 else f"run-{int(time.time() * 1000)}"
    spec = args[spec_idx + 1] if spec_idx >= 0 and spec_idx + 1 < len(args) else ""

    # Quitar los flags y sus valores para quedarnos solo con la tarea.
    excluidos = set()
    for idx in (resume_idx, spec_idx):
        if idx >= 0:
            excluidos.add(idx)
            if idx + 1 < len(args):
                excluidos.add(idx + 1)
    task = "" if resume else " ".join(a for i, a in enumerate(args) if i not in excluidos)

    if not resume and not task:
        print('Uso: run.py [--spec "<contexto>"] "<tarea>" | run.py --resume <thread>', file=sys.stderr)
        sys.exit(2)

    print(f"thread_id: {thread_id}")

    db = os.environ.get("GRAPH_DB", "./checkpoints.db")
    if db == ":memory:":
        _run(InMemorySaver(), thread_id=thread_id, task=task, spec=spec, resume=resume)
        return

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # proporcionado por langgraph-checkpoint-sqlite
    except ImportError:
        print("aviso: langgraph-checkpoint-sqlite no instalado; usando checkpointer en memoria (sin persistencia entre procesos).",
              file=sys.stderr)
        _run(InMemorySaver(), thread_id=thread_id, task=task, spec=spec, resume=resume)
        return

    with SqliteSaver.from_conn_string(db) as saver:
        _run(saver, thread_id=thread_id, task=task, spec=spec, resume=resume)


if __name__ == "__main__":
    main()
