"""客户端只提供问题与条件，不提供正文、下载地址或模型设置。"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, create_model

from archspec.official_qa.condition_fields import CONDITION_KEYS

ShortCondition = Annotated[str, Field(min_length=1, max_length=100)]
SourceIdentity = Annotated[str, Field(pattern=r"^(?:gb55031|official-[a-f0-9]{32})$")]
ProjectConditionIdentity = Annotated[str, Field(pattern=r"^pc_[a-f0-9]{64}$")]


QuestionConditions = create_model(
    "QuestionConditions",
    __module__=__name__,
    __config__=ConfigDict(extra="forbid", str_strip_whitespace=True),
    **{key: (ShortCondition | None, None) for key in CONDITION_KEYS},
)


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1, max_length=500)
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    source_ids: list[SourceIdentity] | None = Field(default=None, min_length=1, max_length=20)
    conditions: QuestionConditions = Field(default_factory=QuestionConditions)
    project_condition_ids: list[ProjectConditionIdentity] | None = Field(default=None, max_length=60)
