DEFAULT_MIN_BUFFER_BLOCK_SIZE = 32_768


def resolve_min_buffer_block_size(config):
    """Read the new packer key while accepting configs written before its move."""
    # Upgraded nodes may still carry this setting under the old ``misc``
    # section. Keep a real default so the first pack after upgrading can never
    # turn size comparisons into ``int < None``.
    return (
        config.get("packer.MIN_BUFFER_BLOCK_SIZE")
        or config.get("MIN_BUFFER_BLOCK_SIZE", DEFAULT_MIN_BUFFER_BLOCK_SIZE)
        or DEFAULT_MIN_BUFFER_BLOCK_SIZE
    )
