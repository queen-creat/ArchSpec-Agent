"""不访问外网的 Agent 行为评测和指标计算。"""

from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field


class AgentEvaluationCase(BaseModel):
    """一个固定评测场景及其人工定义的期望行为。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    allow_materials: bool
    expected_tools: list[str]
    expected_overall_status: str = Field(min_length=1)
    requires_valid_citation: bool
    scripted_model_sequence: list[dict[str, object]]


class AgentEvaluationOutcome(BaseModel):
    """被测 Agent 针对一个场景产生的可观测结果。"""

    model_config = ConfigDict(extra="forbid")

    tools_used: list[str]
    overall_status: str
    citations_valid: bool
    loop_stopped: bool
    model_steps: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class AgentEvaluationMetrics(BaseModel):
    """离线评测汇总；没有样本的指标返回 None。"""

    total: int
    tool_selection_accuracy: float | None
    overall_status_accuracy: float | None
    citation_validity_rate: float | None
    no_evidence_refusal_rate: float | None
    loop_limit_success_rate: float | None
    average_model_steps: float | None
    average_tool_calls: float | None
    average_tokens: float | None


AgentCaseRunner = Callable[[AgentEvaluationCase], AgentEvaluationOutcome]


class AgentEvaluator:
    """根据固定输入计算结果，不发起模型请求，也不产生费用。"""

    def evaluate(
        self,
        cases: Sequence[AgentEvaluationCase],
        runner: AgentCaseRunner,
    ) -> AgentEvaluationMetrics:
        results = [(case, runner(case)) for case in cases]
        total = len(results)

        citation_results = [
            outcome.citations_valid
            for case, outcome in results
            if case.requires_valid_citation
        ]
        refusal_results = [
            outcome.overall_status == case.expected_overall_status
            for case, outcome in results
            if case.category == "REFUSAL"
        ]
        loop_results = [
            outcome.loop_stopped
            for case, outcome in results
            if case.category == "LOOP"
        ]

        return AgentEvaluationMetrics(
            total=total,
            tool_selection_accuracy=self._ratio(
                [
                    set(outcome.tools_used) == set(case.expected_tools)
                    for case, outcome in results
                ]
            ),
            overall_status_accuracy=self._ratio(
                [
                    outcome.overall_status == case.expected_overall_status
                    for case, outcome in results
                ]
            ),
            citation_validity_rate=self._ratio(citation_results),
            no_evidence_refusal_rate=self._ratio(refusal_results),
            loop_limit_success_rate=self._ratio(loop_results),
            average_model_steps=self._average(
                [outcome.model_steps for _, outcome in results]
            ),
            average_tool_calls=self._average(
                [outcome.tool_calls for _, outcome in results]
            ),
            average_tokens=self._average(
                [outcome.total_tokens for _, outcome in results]
            ),
        )

    @staticmethod
    def _ratio(values: Sequence[bool]) -> float | None:
        if not values:
            return None
        return sum(values) / len(values)

    @staticmethod
    def _average(values: Sequence[int]) -> float | None:
        if not values:
            return None
        return sum(values) / len(values)
