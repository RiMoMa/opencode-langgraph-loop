# opencode-langgraph-loop

Bucle de programación autónoma con **LangGraph** y agentes **OpenCode** 100 % locales (Ollama).
Nació para trabajar sobre el fork de [ASAP](https://github.com/RiMoMa/ASAP) (C++/Qt), pero el grafo es casi genérico.

```
preparar_worktree → planner/preflight → programador → post_programador
 → verificacion (cmake + pytest, determinista)
 → revisor (proponente) → revisor_redteam → agregar_revision (juez determinista)
 → router → programador | compuerta_humana (interrupt approve/reject) → END
```

- La **verificación** es la única fuente de verdad; los agentes solo editan y comentan.
- Los **guardrails** son las reglas `permission` de `opencode.json` (`opencode run --auto` aprueba lo no denegado).
- El trabajo se hace en un **git worktree** aparte (`.worktrees/`); la **compuerta humana** pausa el grafo con `interrupt()`.
- Los modelos se declaran en `opencode.json` (Ollama en `localhost:11434`); ajústalos a los que tengas.

## Uso
```bash
pip install -r requirements.txt
langgraph dev                                   # Studio / API (grafo "asap")
python graph/run.py "<tarea>"                   # CLI
python graph/run.py --resume <thread>           # reanudar una pausada
```
Variables: `GRAPH_PYTHON` (intérprete para pytest, por defecto el actual), `GRAPH_DB` (sqlite o `:memory:`).

## Adaptarlo a otro repo
Se ejecuta desde la raíz del repo objetivo. Los flags de `cmake` (`CMAKE_FLAGS`) y la ruta de tests
(`scripts/tests`) en `graph/workflow.py` son los de ASAP: cámbialos por tu comando de build/test.

## Estado
Experimental, sin tests propios. Licencia GPL-2.0.
