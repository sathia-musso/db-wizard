"""
db-wizard - Advanced database copy and migration tool

A multi-database management tool with features for copying,
backing up, and migrating databases. Supports MongoDB and MySQL.
"""

__version__ = "2.4.0"
__author__ = "Sathia Musso"

from .engine import DatabaseEngine, EngineFactory

__all__ = [
    "DatabaseEngine",
    "EngineFactory",
]
