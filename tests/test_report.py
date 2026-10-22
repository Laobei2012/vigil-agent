import pytest

from vigil_agent.agent.report import Report, read_only_command


def mitigation(action, command=None):
    return {"action": action, "risk": "low", "expected_effect": "x", "command": command}


def report(*mitigations):
    return Report(
        mode="deep",
        verdict="v",
        summary="s",
        impact="i",
        hypotheses=[],
        mitigations=list(mitigations),
        confidence="medium",
    )


@pytest.mark.parametrize(
    "command",
    [
        "kubectl -n vigil-chaos get pods,deploy,sts,svc,pvc -o wide",
        "kubectl describe pod -n x -l app=db && kubectl logs -n x -l app=db --previous --tail=50",
        "kubectl get pods -n x | grep db; kubectl get events -n x",
        "flux get kustomizations -A",
        "git log --oneline -- path/",
        "helm history jenkins -n jenkins",
    ],
)
def test_read_only_commands(command):
    assert read_only_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "kubectl -n x apply -f db.yaml",
        "kubectl get pods -n x; kubectl delete pod payments-api -n x",
        "kubectl rollout restart deploy/x",
        "flux reconcile kustomization x --with-source",
        "git revert abc && git push",
        "kubectl -n x scale deploy/x --replicas=2",
        None,
        "",
    ],
)
def test_commands_that_change_something_or_are_unknown(command):
    assert not read_only_command(command)


def test_read_only_steps_move_below_the_steps_that_act():
    r = report(
        mitigation("Inspect the namespace", "kubectl -n x get pods,svc"),
        mitigation("Restore the db Service", "kubectl -n x apply -f db.yaml"),
        mitigation("Read the db logs", "kubectl logs -n x db-0"),
        mitigation("Revert the commit", "git revert abc && git push"),
    )
    assert [m.action for m in r.mitigations] == [
        "Restore the db Service",
        "Revert the commit",
        "Inspect the namespace",
        "Read the db logs",
    ]
    # Validating the stored report again keeps the order.
    again = Report.model_validate(r.model_dump())
    assert [m.action for m in again.mitigations] == [m.action for m in r.mitigations]


def test_order_stays_when_every_step_only_reads():
    r = report(mitigation("a", "kubectl get pods"), mitigation("b", "kubectl describe pod x"))
    assert [m.action for m in r.mitigations] == ["a", "b"]
