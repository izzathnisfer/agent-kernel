"""Timer providers implementing the ``Scheduler`` contract.

Providers live with their capability rather than under ``deployment/`` (the
``sandbox/providers/ec2_ssm.py`` precedent), and are imported only when configuration
selects them.
"""
