"""Slot policies (#669): `access`, `tunnel_auth` and `wake` on a `Service.Api.Slot`."""
import os
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.utils import policies
    from src.utils import shared_filesystems as sf
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

DOCS = os.path.join(os.path.dirname(__file__), "..", "docs", "policies")


def _slot(port=8333, **settings):
    slot = celaut.Service.Api.Slot(port=port)
    for name, value in settings.items():
        policies.set_policy(slot.policies, policies.make(name, value.encode()))
    return slot


def _service(*slots):
    service = celaut.Service()
    service.api.slot.extend(slots)
    return service


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class SlotSettingsTests(unittest.TestCase):

    def test_the_formal_files_are_the_bytes_of_the_code(self):
        for name, formal in (
            ("access", policies.ACCESS_FORMAL),
            ("tunnel_auth", policies.TUNNEL_AUTH_FORMAL),
            ("wake", policies.WAKE_FORMAL),
        ):
            with open(os.path.join(DOCS, f"{name}.formal"), "rb") as f:
                self.assertEqual(f.read(), formal, name)

    def test_a_slot_without_policies_has_the_defaults(self):
        self.assertEqual(
            policies.slot_access(_slot()), policies.SlotAccess("any", "token", "never")
        )

    def test_each_valid_value_is_read(self):
        got = policies.slot_access(_slot(access="tunnel", tunnel_auth="address", wake="on_tunnel"))
        self.assertEqual(got, policies.SlotAccess("tunnel", "address", "on_tunnel"))
        for value in ("direct", "tunnel", "any"):
            self.assertEqual(policies.slot_access(_slot(access=value)).access, value)

    def test_a_value_that_is_not_valid_is_refused(self):
        for name, value in (("access", "open"), ("tunnel_auth", "none"), ("wake", "always")):
            with self.assertRaises(policies.PolicyError, msg=name):
                policies.slot_access(_slot(**{name: value}))

    def test_wake_on_tunnel_with_access_direct_is_refused(self):
        with self.assertRaises(policies.PolicyError):
            policies.slot_access(_slot(access="direct", wake="on_tunnel"))

    def test_tunnel_auth_address_with_access_direct_is_refused(self):
        with self.assertRaises(policies.PolicyError):
            policies.slot_access(_slot(access="direct", tunnel_auth="address"))

    def test_a_setting_of_another_place_is_refused_on_a_slot(self):
        slot = celaut.Service.Api.Slot(port=1)
        slot.policies.append(policies.make(policies.READ_MODE, b"ro"))
        with self.assertRaises(policies.PolicyError):
            policies.slot_access(slot)

    def test_a_setting_of_a_slot_is_refused_in_init(self):
        with self.assertRaises(policies.PolicyError):
            policies.check_list(
                [policies.make(policies.WAKE, b"never")], policies.PLACE_INIT, "init"
            )

    def test_an_unknown_formal_and_a_repeated_one_are_refused(self):
        slot = celaut.Service.Api.Slot(port=1)
        slot.policies.append(celaut.Policy(formal=b"other=1", value=b"x"))
        with self.assertRaises(policies.PolicyError):
            policies.slot_access(slot)
        twice = celaut.Service.Api.Slot(port=1)
        twice.policies.extend([policies.make(policies.WAKE, b"never")] * 2)
        with self.assertRaises(policies.PolicyError):
            policies.slot_access(twice)

    def test_a_service_with_a_bad_slot_is_refused_when_it_is_read_for_launch(self):
        with self.assertRaises(policies.PolicyError):
            sf.declarations_for_service(_service(_slot(wake="always")))

    def test_a_service_with_good_slots_is_accepted(self):
        sf.declarations_for_service(_service(_slot(wake="on_tunnel"), _slot(9, access="any")))


def _packed(api_item):
    from src.packers.zip_with_dockerfile import ZipContainerPacker
    packer = ZipContainerPacker.__new__(ZipContainerPacker)
    packer.json = {"api": [dict(api_item, port=8333)]}
    packer.service = celaut.Service()
    packer.parseApi()
    return packer.service.api.slot[0]


@unittest.skipIf(IMPORT_ERROR is not None, f"imports unavailable: {IMPORT_ERROR}")
class PackerTests(unittest.TestCase):

    def test_policies_of_service_json_become_the_policies_of_the_slot(self):
        slot = _packed({"policies": {"wake": "on_tunnel", "tunnel_auth": "address"}})
        self.assertEqual(
            policies.slot_access(slot), policies.SlotAccess("any", "address", "on_tunnel")
        )
        formals = [bytes(p.formal) for p in slot.policies]
        self.assertEqual(formals, sorted(formals))

    def test_a_slot_without_policies_has_none(self):
        self.assertEqual(len(_packed({}).policies), 0)

    def test_the_packer_refuses_what_a_node_refuses(self):
        for bad in ({"wake": "always"}, {"access": "direct", "wake": "on_tunnel"},
                    {"other": "x"}, {"wake": 1}):
            with self.assertRaises(ValueError, msg=str(bad)):
                _packed({"policies": bad})
        with self.assertRaises(ValueError):
            _packed({"policies": ["wake"]})


if __name__ == "__main__":
    unittest.main()
