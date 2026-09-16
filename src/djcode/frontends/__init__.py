"""Front-ends for DJcode.

Each package here is a CONSUMER of ``djcode.core`` -- never the other way round.
``tests/test_headless_purity.py`` enforces that: nothing reachable from
``djcode.core`` may import anything under ``djcode.frontends``.
"""
