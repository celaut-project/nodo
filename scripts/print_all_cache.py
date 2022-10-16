from src.manager.system_cache import SystemCache

system_cache = SystemCache()
print('\nSystem cache -> ', system_cache.system_cache)
print('\nClients -> ', system_cache.clients)
print('\nTotal deposited on other peers -> ', system_cache.total_deposited_on_other_peers)
print('\nClietns on other peers -> ', system_cache.clients_on_other_peers)
print('\nContainer cache -> ', system_cache.container_cache)
print('\nCache service perspective -> ', system_cache.cache_service_perspective)
print('\nExternal token map -> ', system_cache.external_token_hash_map)