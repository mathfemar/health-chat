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


async def call(name: str, args: dict, ctx: dict) -> Any:
    """Invoca uma tool pelo nome. Passa ctx (user_id, conversation_id, etc)
    como kwarg especial."""
    t = get(name)
    if not t:
        raise ValueError(f"Tool desconhecida: {name}")
    return await t.func(ctx=ctx, **args)
