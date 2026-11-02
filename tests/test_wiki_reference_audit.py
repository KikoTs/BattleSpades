"""Offline references cannot execute code or silently claim gameplay parity."""
from scripts.wiki_reference_audit import archive_name, literal_claims


def test_mac_archive_names_resolve_windows_colons_and_encoded_subpages():
    assert archive_name("User:Marisa_Kirisame/Weapon_Stats") == "User_Marisa_Kirisame%2FWeapon_Stats"
    assert archive_name("***SNIPER_WARS***") == "___SNIPER_WARS___"
    assert "/" not in archive_name("../../arbitrary/file")


def test_only_literal_numeric_assignments_are_claims_not_source_to_execute():
    source = """ROCKET_DAMAGE = 40
IGNORE = __import__('os').system('do not execute this')
EXPRESSION = 10 * 20
NEGATIVE = -0.125 # a literal
PREFIX = 40; MORE = 20
"""
    assert list(literal_claims(source)) == [("ROCKET_DAMAGE", 40), ("NEGATIVE", -.125)]
