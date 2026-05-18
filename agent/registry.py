"""Registry de tools: decorator @tool + geração de schema OpenAI/Gemma."""
from dataclasses import dataclass
from typing import Callable, Any


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict          # JSON schema
    func: Callable            # função async


_REGISTRY: dict[str, ToolDef] = {}


def tool(name: str, description: str, parameters: dict) -> Callable:
    """Registra uma função async como tool disponível pro agente.

    parameters é JSON Schema completo (tipo objeto). Escrevemos à mão
    pra ter controle total — introspecção de type hints é frágil pra
    tipos complexos.
    """
    def decorator(func: Callable) -> Callable:
        _REGISTRY[name] = ToolDef(name=name, description=description,
                                  parameters=parameters, func=func)
        return func
    return decorator


def all_tools() -> list[ToolDef]:
    return list(_REGISTRY.values())


def get(name: str) -> ToolDef | None:
    return _REGISTRY.get(name)


def openai_schema() -> list[dict]:
    """Formato esperado pelo OpenRouter/OpenAI/Gemma 4 tool calling."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in _REGISTRY.values()
    ]


def _coerce(value, json_type: str):
    """Tenta converter value para o tipo declarado no JSON schema.
    Lida com lixo do LLM (control tokens, espaços, vírgulas decimais).
    Levanta ValueError se impossível."""
    if value is None:
        return None
    if json_type == "integer":
        if isinstance(value, bool): return int(value)
        if isinstance(value, int):  return value
        if isinstance(value, float): return int(value)
        # string: pega só os dígitos iniciais
        s = str(value).strip()
        import re
        m = re.match(r"-?\d+", s)
        if not m:
            raise ValueError(f"não consegui ler integer de {value!r}")
        return int(m.group(0))
    if json_type == "number":
        if isinstance(value, (int, float)): return float(value)
        s = str(value).strip().replace(",", ".")
        import re
        m = re.match(r"-?\d+(?:\.\d+)?", s)
        if not m:
            raise ValueError(f"não consegui ler number de {value!r}")
        return float(m.group(0))
    if json_type == "boolean":
        if isinstance(value, bool): return value
        s = str(value).strip().lower()
        return s in ("true", "1", "yes", "sim")
    if json_type == "string":
        return str(value)
    # array, object, ou unknown: passa direto
    return value


def _sanitize_args(tool_def: ToolDef, args: dict) -> tuple[dict, list[str]]:
    """Valida required, coage tipos. Retorna (args_validados, lista_erros).
    Se erros existem, ainda retorna args parciais pra log.
    """
    schema = tool_def.parameters or {}
    props = schema.get("properties", {}) or {}
    required = set(schema.get("required", []))
    errors: list[str] = []
    out = {}
    args = args or {}

    # Coage tipos pra cada arg presente
    for k, v in args.items():
        prop = props.get(k)
        if not prop:
            # arg desconhecida — passa adiante (LLM pode inventar field, melhor ignorar que errar)
            out[k] = v
            continue
        json_type = prop.get("type", "string")
        try:
            out[k] = _coerce(v, json_type)
        except ValueError as e:
            errors.append(f"campo '{k}': {e}")

    # Verifica required
    missing = [r for r in required if r not in out or out[r] is None or out[r] == ""]
    if missing:
        errors.append(f"campos obrigatórios faltando: {missing}")

    return out, errors


async def call(name: str, args: dict, ctx: dict) -> Any:
    """Invoca uma tool pelo nome. Passa ctx (user_id, conversation_id, etc)
    como kwarg especial. Não levanta — retorna dict com erro pro LLM se algo falhar."""
    t = get(name)
    if not t:
        return {"error": f"Tool desconhecida: {name}"}
    clean_args, errors = _sanitize_args(t, args)
    if errors:
        return {"error": "argumentos inválidos", "details": errors,
                "expected_schema": t.parameters}
    try:
        return await t.func(ctx=ctx, **clean_args)
    except TypeError as e:
        # ex: kwarg inesperado
        return {"error": f"erro chamando {name}: {e}"}
