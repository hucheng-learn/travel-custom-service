from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)


class StartStep(StepBase):
    type: Literal["start"]
    next: str


class CollectStep(StepBase):
    type: Literal["collect"]
    slot: str
    prompt: str = Field(min_length=1)
    next: str


class ActionStep(StepBase):
    type: Literal["action"]
    action: str
    routes: dict[str, str]


class ResponseStep(StepBase):
    type: Literal["response"]
    template: str
    next: str
    pause: bool = False


class EndStep(StepBase):
    type: Literal["end"]


# 根据 type 选择字段规则，例如 collect 必须有 prompt，而 end 不接受 next。
type Step = Annotated[StartStep | CollectStep | ActionStep | ResponseStep | EndStep, Field(discriminator="type")]


class SlotDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["order_ref", "travel_date", "product_request"]


class FlowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[Step] = Field(min_length=2)


class FlowFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slots: dict[str, SlotDefinition]
    flows: dict[str, FlowDefinition] = Field(min_length=1)


@dataclass(frozen=True)
class LoadedFlow:
    start_id: str
    steps: dict[str, Step]
    version: str


def load_flows(
    path: Path,
    *,
    action_routes: dict[str, frozenset[str]],
    templates: set[str],
) -> dict[str, LoadedFlow]:
    config = FlowFile.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    flows = {}
    for flow_id, definition in config.flows.items():
        by_id = {step.id: step for step in definition.steps}
        starts = [step.id for step in definition.steps if isinstance(step, StartStep)]
        if len(by_id) != len(definition.steps) or len(starts) != 1:
            raise ValueError(f"{flow_id}：步骤 ID 必须唯一，且必须有一个 start")

        # 启动时把编号、槽位和分支校验完，避免聊到一半才发现 YAML 写错。
        targets_by_step: dict[str, list[str]] = {}
        for step in definition.steps:
            if isinstance(step, CollectStep) and step.slot not in config.slots:
                raise ValueError(f"{flow_id}.{step.id}：未定义槽位 {step.slot}")
            if isinstance(step, ResponseStep) and step.template not in templates:
                raise ValueError(f"{flow_id}.{step.id}：未注册回复模板 {step.template}")
            if isinstance(step, ActionStep):
                if step.action not in action_routes or step.routes.keys() != action_routes[step.action]:
                    raise ValueError(f"{flow_id}.{step.id}：Action 或结果分支与注册信息不一致")
                targets = list(step.routes.values())
            elif isinstance(step, EndStep):
                targets = []
            else:
                targets = [step.next]
            if not set(targets) <= by_id.keys():
                raise ValueError(f"{flow_id}.{step.id}：跳转到了不存在的步骤")
            if isinstance(step, ResponseStep) and step.pause and not isinstance(by_id[step.next], CollectStep):
                raise ValueError(f"{flow_id}.{step.id}：暂停后必须回到收集信息的步骤")
            targets_by_step[step.id] = targets

        reachable: set[str] = set()
        pending = [starts[0]]
        while pending:
            step_id = pending.pop()
            if step_id not in reachable:
                reachable.add(step_id)
                pending.extend(targets_by_step[step_id])
        if reachable != by_id.keys() or not any(isinstance(by_id[step_id], EndStep) for step_id in reachable):
            raise ValueError(f"{flow_id}：请检查无法到达的步骤和结束节点")

        # 对解析后的配置取版本号，改 YAML 注释不会让进行中的会话失效。
        version = sha256(definition.model_dump_json().encode("utf-8")).hexdigest()
        flows[flow_id] = LoadedFlow(starts[0], by_id, version)
    return flows
