from dataclasses import dataclass

@dataclass(frozen=True)
class CardDefinition:
    id: str
    operation: str
    capabilities: tuple[str, ...]
    description: str
    physical_basis: str
    source: tuple[str, ...]
    continuous: bool = False
    outputs: tuple[str, ...] = ()
    version: int = 1

