from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentModelProfile:
    key: str
    name: str
    preferred_families: tuple[str, ...]
    capabilities: tuple[str, ...]
    reason: str


AGENT_MODEL_PROFILES = (
    AgentModelProfile(
        "director",
        "总导演 Agent",
        ("deepseek", "qwen", "glm"),
        ("reasoning", "long_context"),
        "需要全局规划、冲突判断和长上下文统筹",
    ),
    AgentModelProfile(
        "concept",
        "策划 Agent",
        ("deepseek", "qwen", "glm"),
        ("reasoning",),
        "需要受众、钩子和商业潜力推演",
    ),
    AgentModelProfile(
        "script",
        "编剧 Agent",
        ("deepseek", "qwen", "glm"),
        ("reasoning", "long_context"),
        "需要长文本结构、人物动机和对白一致性",
    ),
    AgentModelProfile(
        "assets",
        "美术 Agent",
        ("qwen", "glm", "deepseek"),
        ("vision",),
        "需要识别角色、场景和道具的视觉一致性",
    ),
    AgentModelProfile(
        "storyboard",
        "分镜 Agent",
        ("qwen", "glm", "deepseek"),
        ("vision", "reasoning"),
        "需要同时理解剧本、构图和镜头连续性",
    ),
    AgentModelProfile(
        "video",
        "摄影 Agent",
        ("qwen", "glm", "deepseek"),
        ("vision",),
        "需要检查生成画面的主体、动作和稳定性",
    ),
    AgentModelProfile(
        "audio",
        "声音 Agent",
        ("qwen", "glm", "deepseek"),
        ("audio", "long_context"),
        "需要理解对白表演、声纹和整体声场",
    ),
    AgentModelProfile(
        "edit",
        "剪辑 Agent",
        ("qwen", "glm", "deepseek"),
        ("vision", "long_context"),
        "需要跨镜头理解叙事节奏和声画关系",
    ),
    AgentModelProfile(
        "quality",
        "监制 Agent",
        ("deepseek", "qwen", "glm"),
        ("reasoning", "vision"),
        "需要执行多维质检、合规判断和问题归因",
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

