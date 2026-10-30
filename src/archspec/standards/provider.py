"""未来接入住建部、国家标准平台或授权文库的统一协议。"""

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from archspec.standards.models import StandardLifecycleStatus


@dataclass(frozen=True)
class OfficialStandardReference:
    standard_code: str
    title: str
    authority: str
    source_url: str
    lifecycle_status: StandardLifecycleStatus
    published_at: date | None
    effective_at: date | None
    replaced_by: str | None


class OfficialStandardsProvider(Protocol):
    """只约定题录能力；具体来源不得泄漏到审查业务。"""

    def search(self, query: str) -> list[OfficialStandardReference]: ...

    def get(self, standard_code: str) -> OfficialStandardReference | None: ...
