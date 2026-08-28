from src.utils.packer_config import resolve_min_buffer_block_size


class FakeConfig:
    def __init__(self, values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_new_scoped_block_threshold_wins():
    config = FakeConfig({
        "packer.MIN_BUFFER_BLOCK_SIZE": 32_768,
        "MIN_BUFFER_BLOCK_SIZE": 10_000_000,
    })
    assert resolve_min_buffer_block_size(config) == 32_768


def test_pre_move_config_uses_legacy_threshold():
    config = FakeConfig({"MIN_BUFFER_BLOCK_SIZE": 10_000_000})
    assert resolve_min_buffer_block_size(config) == 10_000_000


def test_missing_threshold_has_a_numeric_default():
    assert resolve_min_buffer_block_size(FakeConfig({})) == 32_768
