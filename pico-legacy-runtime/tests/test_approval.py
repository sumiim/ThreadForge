from pico.approval import (
    AcceptEditsApprovalStrategy,
    ApprovalOutcome,
    ApprovalRequest,
    AutoApprovalStrategy,
    NeverApprovalStrategy,
    PermissionMode,
    strategy_for_mode,
    strategy_for_policy,
)


def _req(name):
    return ApprovalRequest(name=name, args={}, tool_call_id="c1")


def test_legacy_policy_maps_auto_never_ask():
    from pico.approval import AskApprovalStrategy

    assert isinstance(strategy_for_policy("auto"), AutoApprovalStrategy)
    assert isinstance(strategy_for_policy("never"), NeverApprovalStrategy)
    # ask / unknown fall back to the interactive strategy (not a fixed outcome)
    assert isinstance(strategy_for_policy("ask"), AskApprovalStrategy)
    assert isinstance(strategy_for_policy("bogus"), AskApprovalStrategy)


def test_permission_modes_map_to_strategies():
    assert isinstance(strategy_for_mode(PermissionMode.BYPASS), AutoApprovalStrategy)
    assert isinstance(strategy_for_mode(PermissionMode.PLAN), NeverApprovalStrategy)
    assert isinstance(strategy_for_mode(PermissionMode.ACCEPT_EDITS), AcceptEditsApprovalStrategy)
    # default falls through to the fallback (Ask), not a fixed outcome
    default = strategy_for_mode(PermissionMode.DEFAULT)
    assert not isinstance(
        default, (AutoApprovalStrategy, NeverApprovalStrategy, AcceptEditsApprovalStrategy)
    )


def test_accept_edits_auto_approves_edits_but_delegates_shell():
    class _Recording:
        def __init__(self):
            self.calls = []

        def decide(self, request):
            self.calls.append(request.name)
            return ApprovalOutcome.REJECTED

    fallback = _Recording()
    strategy = AcceptEditsApprovalStrategy(fallback)

    assert strategy.decide(_req("write_file")) is ApprovalOutcome.APPROVED
    assert strategy.decide(_req("patch_file")) is ApprovalOutcome.APPROVED
    # run_shell (non-edit risky) delegates to the fallback strategy
    assert strategy.decide(_req("run_shell")) is ApprovalOutcome.REJECTED
    assert fallback.calls == ["run_shell"]
