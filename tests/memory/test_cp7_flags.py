"""C-P7 retires only switches with no runtime consumer."""

from nanobot.config.schema import Phase6Config, Phase6EvolutionConfig
from nanobot.memory.continuous import Phase6RuntimeConfig


def test_dead_shadow_and_case_promotion_flags_are_absent_but_observation_groups_remain():
    assert "shadow_mode" not in Phase6EvolutionConfig.model_fields
    assert "case_candidate_promotion" not in Phase6EvolutionConfig.model_fields
    assert "case_candidate_promotion" not in Phase6Config.model_fields
    assert "case_candidate_promotion" not in Phase6RuntimeConfig.__dataclass_fields__
    assert "observation_groups" in Phase6EvolutionConfig.model_fields


def test_legacy_unknown_flags_do_not_create_runtime_dto_consumers():
    evolution = Phase6EvolutionConfig.model_validate({
        "shadowMode": True, "caseCandidatePromotion": True,
        "observationGroups": ["group-retained"],
    })
    config = Phase6RuntimeConfig.from_config(type("Config", (), {"evolution": evolution})())
    assert not hasattr(config, "shadow_mode")
    assert not hasattr(config, "case_candidate_promotion")
    assert evolution.observation_groups == ["group-retained"]
