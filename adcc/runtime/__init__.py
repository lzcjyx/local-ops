"""Pure runtime compatibility helpers extracted during ADCC M1.

This package deliberately contains no process execution or platform selection.
The temporary ``server.py`` compatibility entrypoint remains responsible for
collecting OS facts and delegates their parsing and projection here.
"""
