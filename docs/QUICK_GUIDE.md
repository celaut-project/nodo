# NODO QUICK START

1. Setup
   nodo tui        (Config page — edits config.yaml, backup included)

2. Get a service
   nodo download <url>
   OR
   nodo import <path>

3. Build your own
   nodo pack <folder>

4. Share it
   nodo publish <service>

5. Run it
   nodo execute <service>

EXTRAS

Remote run:
   nodo execute --remote <service>

Export package (importable .celaut.bee — share this, feed it to `nodo import`):
   nodo export <service> <dir>

Raw .celaut (verify hash only — NOT importable):
   nodo export <service> <dir> --raw