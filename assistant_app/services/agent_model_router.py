from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentModelProfile:
    key: str
    name: str
    preferred_families: tuple[str, ...]
    capabilities: tuple[str, ...]
    reason: str
    executor: str = "model"
    runtime_name: str | None = None


AGENT_MODEL_PROFILES = (
    AgentModelProfile(
        "story",
        "故事 Agent",
        ("deepseek", "qwen", "glm"),
        ("reasoning", "long_context"),
        "负责把创意收敛为受众、人物、节拍、对白和完整剧本",
    ),
    AgentModelProfile(
        "visual",
        "视觉 Agent",
        ("qwen", "glm", "deepseek"),
        ("vision", "reasoning"),
        "负责连续性资产、分镜、视频提示词、逐镜台词和字幕",
    ),
    AgentModelProfile(
        "media",
        "媒体制作 Agent",
        (),
        (),
        "实际调用视频和语音渠道，并使用 FFmpeg 混音、烧录字幕和合片",
        executor="tool",
        runtime_name="video+speech+ffmpeg",
    ),
    AgentModelProfile(
        "quality",
        "质检 Agent",
        (),
        (),
        "用 ffprobe 和结构化规则验证画面、语音、字幕、时长和最终文件",
        executor="tool",
        runtime_name="ffprobe+quality-rules",
    ),
)


def _model_family(model_name: str) -> str:
    normalized = model_name.lower()
    if "deepseek" in normalized:
        return "deepseek"
    if "qwen" in normalized or "qwq" in normalized:
        return "qwen"
    if "glm" in normalized or "chatglm" in normalized:
        return "glm"
    return "other"


def _model_capabilities(model_name: str) -> set[str]:
    normalized = model_name.lower()
    capabilities: set[str] = set()
    if any(token in normalized for token in ("reason", "thinking", "r1", "qwq", "qwen3")):
        capabilities.add("reasoning")
    if any(token in normalized for token in ("vl", "vision", "4v", "omni")):
        capabilities.add("vision")
    if any(token in normalized for token in ("audio", "omni")):
        capabilities.add("audio")
    if any(token in normalized for token in ("long", "128k", "256k", "1m")):
        capabilities.add("long_context")
    return capabilities


def _score_model(model_name: str, profile: AgentModelProfile) -> tuple[int, str, set[str]]:
    family = _model_family(model_name)
    capabilities = _model_capabilities(model_name)
    score = 0
    if family in profile.preferred_families:
        score += 90 - profile.preferred_families.index(family) * 15
    else:
        score += 5
    matched_capabilities = capabilities.intersection(profile.capabilities)
    score += len(matched_capabilities) * 60
    if "reasoning" in profile.capabilities and family == "deepseek":
        score += 15
    if "vision" in profile.capabilities and family in {"qwen", "glm"}:
        score += 10
    return score, family, matched_capabilities


def route_agent_models(available_models: list[str]) -> list[dict[str, object]]:
    unique_models = list(dict.fromkeys(item.strip() for item in available_models if item.strip()))
    assignments: list[dict[str, object]] = []
    for profile in AGENT_MODEL_PROFILES:
        if profile.executor == "tool":
            assignments.append(
                {
                    "agent": profile.key,
                    "agent_name": profile.name,
                    "model": profile.runtime_name,
                    "family": "tool",
                    "status": "tool",
                    "reason": profile.reason,
                    "matched_capabilities": [],
                    "score": 0,
                }
            )
            continue
        ranked = sorted(
            (
                (_score_model(model_name, profile), model_name)
                for model_name in unique_models
            ),
            key=lambda item: (item[0][0], item[1]),
            reverse=True,
        )
        if not ranked:
            assignments.append(
                {
                    "agent": profile.key,
                    "agent_name": profile.name,
                    "model": None,
                    "family": None,
                    "status": "unavailable",
                    "reason": "当前没有可用模型",
                }
            )
            continue
        (score, family, matched_capabilities), model_name = ranked[0]
        is_primary = family in {"glm", "deepseek", "qwen"}
        assignments.append(
            {
                "agent": profile.key,
                "agent_name": profile.name,
                "model": model_name,
                "family": family,
                "status": "matched" if is_primary else "fallback",
                "reason": profile.reason,
                "matched_capabilities": sorted(matched_capabilities),
                "score": score,
            }
        )
    return assignments
