"""Plugin-owned configuration validation and prepared-plan invalidation."""
from dataclasses import replace
import json
from pathlib import Path

import pytest
from culsma_opentrons.config import DeckConfiguration
from culsma_opentrons.policy import TransferPolicy
from .test_operations import prepare_fixture


class ConfigurationProfileFixture:
    def raw(self):
        return json.loads((Path(__file__).resolve().parents[1]/'examples/deck.json').read_text())


class TestConfigurationProfiles:
    @pytest.mark.parametrize('field,value', [
        ('api_level', '2.19'), ('api_level', '2.21'),
        ('mount', 'center'), ('mount', None),
        ('pipette', 'p1000_multi_gen2'), ('pipette', 'p1000_single_gen2'),
        ('schema_version', 3), ('schema_version', True),
    ])
    def test_unsupported_setup_is_rejected(self, field, value):
        raw = ConfigurationProfileFixture().raw()
        raw[field] = value
        with pytest.raises(ValueError):
            DeckConfiguration.from_dict(raw)

    @pytest.mark.parametrize('field', ['aspirate_flow_rate_uL_s', 'dispense_flow_rate_uL_s'])
    @pytest.mark.parametrize('value', [-1, float('inf'), -float('inf'), '3'])
    def test_flow_rate_domain_rejects_invalid_values(self, field, value):
        raw = ConfigurationProfileFixture().raw()
        raw['transfer_policy'] = {field: value}
        with pytest.raises(ValueError):
            DeckConfiguration.from_dict(raw)

    @pytest.mark.parametrize('change', ['mount', 'pipette', 'tiprack-slot', 'labware-slot', 'binding', 'aspirate', 'dispense', 'stroke'])
    def test_configuration_revision_covers_execution_profile(self, change):
        raw = ConfigurationProfileFixture().raw()
        original = DeckConfiguration.from_dict(raw)
        if change == 'mount':
            raw['mount'] = 'left'
        elif change == 'pipette':
            raw['pipette'] = 'p20_single_gen2'
            raw['tiprack']['load_name'] = 'opentrons_96_tiprack_20ul'
        elif change == 'tiprack-slot':
            raw['tiprack']['slot'] = '8'
        elif change == 'labware-slot':
            raw['labware']['tubes']['slot'] = '7'
        elif change == 'binding':
            key = next(iter(raw['bindings']))
            raw['bindings'][key]['well'] = 'D6'
        else:
            raw['transfer_policy'] = {'stroke_mode': 'balanced_split'} if change == 'stroke' else {f'{change}_flow_rate_uL_s': 3.0}
        changed = DeckConfiguration.from_dict(raw)
        assert changed.fingerprint() != original.fingerprint()
        assert DeckConfiguration.from_dict(changed.snapshot()).fingerprint() == changed.fingerprint()
        assert original.snapshot() == DeckConfiguration.from_dict(ConfigurationProfileFixture().raw()).snapshot()

    @pytest.mark.parametrize('field,value', [
        ('stroke_mode', 'balanced_split'),
        ('aspirate_flow_rate_uL_s', 3.0),
        ('dispense_flow_rate_uL_s', 4.0),
    ])
    def test_prepared_policy_change_rejects_without_actions(self, backend, field, value):
        driver, step = prepare_fixture('multi-source', backend)
        prepared = driver.transfers.prepare_transfer(driver.mapper.from_step(step))
        driver.bindings.config = replace(driver.bindings.config, transfer_policy=replace(TransferPolicy(), **{field: value}))
        receipt = driver.transfers.execute_transfer(prepared)
        assert not receipt.ok and receipt.code == 'DRV_OT_PREPARATION_STALE'
        assert receipt.phase == 'rejected' and receipt.action_results == ()
        assert not backend.protocol.commands()
        assert backend.available_tips() == 96
        assert driver.transfers.execute_transfer(prepared).code == 'DRV_OT_PREPARATION_INVALID'
