"""``build_report``: what a service asks of the person who runs it."""
from protos import celaut_pb2 as celaut
from src.utils import keyvalue
from src.utils.service_report import SECTION_KEYS, build_report


def _service(**kw) -> celaut.Service:
    service = celaut.Service(prose="demo")
    service.container.architecture.tags.append("linux/amd64")
    service.container.init.entry_path.extend(["/bin/run"])
    r = service.container.resources
    r.at_init.cpu_quota, r.at_init.cpu_period = 150000, 100000
    r.at_init.mem_limit = 512 * 1024 ** 2
    r.at_most.mem_limit = 1024 ** 3
    entry = service.container.environment_variables.add(key="API_KEY")
    entry.value.tags.append("secret")
    entry.value.prose = "key for the upstream"
    slot = service.api.slot.add(port=8080)
    slot.transport.tags.append("tcp")
    for network in kw.get("networks", ()):
        service.network.add(tags=[network], prose="why")
    return service


def _by_level(report):
    return {(a["level"], a["topic"]) for a in report["assumptions"]}


def test_isolated_service_says_so():
    report = build_report("abc", _service())
    assert ("ok", "Network") in _by_level(report)
    assert [s["key"] for s in report["sections"]] == list(SECTION_KEYS)


def test_open_egress_is_a_risk_and_a_host_is_a_warning():
    report = build_report("abc", _service(networks=["*", "api.example.com"]))
    levels = _by_level(report)
    assert ("risk", "Network") in levels and ("warn", "Network") in levels
    assert ("ok", "Network") not in levels


def test_env_arch_and_resources_are_shown():
    report = build_report("abc", _service())
    sections = {s["key"]: "\n".join(s["lines"]) for s in report["sections"]}
    assert "API_KEY" in sections["env"] and "secret" in sections["env"]
    assert "linux/amd64" in sections["machine"]
    assert "1.5 CPU cores" in sections["machine"] and "512.00 MB memory" in sections["machine"]
    assert report["env"][0]["name"] == "API_KEY"


def test_unsupported_architecture_is_a_risk_for_this_node():
    report = build_report("abc", _service(), node={"supported_architectures": ["linux/arm64"]})
    assert ("risk", "This node") in _by_level(report)


def test_no_resources_is_not_hidden():
    service = _service()
    service.container.ClearField("resources")
    assert ("warn", "Cost") in _by_level(build_report("abc", service))


def test_dependencies_are_a_warning():
    service = _service()
    workload = service.initial_environment_workload.add().any_of.add().workloads.add(count=2)
    workload.dependency.hash.add(value=b"\x01" * 32)
    report = build_report("abc", service)
    assert ("warn", "Dependencies") in _by_level(report)
    deps = [s for s in report["sections"] if s["key"] == "deps"][0]["lines"]
    assert any("2 x 010101010101" in line for line in deps)
