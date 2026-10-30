"""Brokered AWG channel; all mutations carry the laser owner's projection."""
from ldcon.core.instrument_client import BrokerTransport
from ldcon.drivers.siglent import AWGChannel, fields, number


class PreparationInterrupted(Exception):
    """Preparation stopped at a known boundary, with no unknown in-flight write."""


class PreparingChannel(AWGChannel):
    def __init__(self, port, config):
        super().__init__(port.io, config)
        self.port = port

    def command(self, command):
        self.port.checkpoint()
        self.port.changed = True
        return super().command(command)

    def upload(self, *args):
        self.port.checkpoint()
        self.port.changed = True
        return super().upload(*args)


class Port:
    def __init__(self, config, authority):
        self.io = BrokerTransport(config, owner=authority.system_id + ' DLC sweep')
        self.checkpoint = lambda: None
        self.channel = PreparingChannel(self, config)
        self.changed = False
        try:
            self.before = self.channel.snapshot(allow_output_off=True)
            wave = self.before['wave']
            if wave.get('WVTP') != 'DC' or abs(number(wave['OFST'])) > 1e-6:
                raise ValueError('Commission the dedicated AWG channel to zero DC before use')
            self.was_on = self.before['OUTP'].split(' ', 1)[-1].split(',')[0] == 'ON'
            n = str(config['channel'])
            constraints = dict(channels={n:dict(bounds=[config['min_v'], config['max_v']],
                max_vpp=config['max_vpp'], rest_v=0., allow_output_enable=True)}, max_points=8_000_000,
                cleanup_operations=[])
            self.io.configure_projection(authority, constraints)
        except BaseException:
            # No physical write has been attempted in constructor preflight.
            self.io.close()
            raise

    def arm(self, record, role, slot):
        resolved = self.channel.resolve(record.waves[role], self.before)
        # DLC ARC remains disabled while the carrier and burst state change.
        self.channel.command('OUTP OFF')
        name = self.channel.upload(record.waves[role], record.metadata, resolved, slot)
        receipt = self.channel.arm(name, record.metadata, resolved, source='EXT', cycles=1)
        self.channel.command('OUTP ON')
        if not self.channel.query('OUTP?').split(' ', 1)[-1].startswith('ON,'):
            raise RuntimeError('AWG output enable was not confirmed')
        self.channel.verify(name, record.metadata, resolved)
        self.armed_record = (name, record.metadata, dict(resolved))
        return dict(trigger_source='EXT', **receipt)

    def rearm_offset(self, correction_v):
        """One measured carrier correction, with output OFF during re-arming.

        BSWV changes can exit burst. Reapply/verify the entire external burst
        configuration before enabling output; never issue a trigger or upload.
        """
        name, metadata, original = self.armed_record
        resolved = dict(original, offset_v=original['offset_v']+correction_v)
        # Same electrical preflight as original arm, before the first write.
        self.channel.resolve(dict(offset_v=resolved['offset_v'],
            amplitude_vpp=resolved['amplitude_vpp']), self.before)
        self.channel.command('OUTP OFF')
        receipt = self.channel.arm(name, metadata, resolved, source='EXT', cycles=1)
        self.channel.command('OUTP ON')
        if not self.channel.query('OUTP?').split(' ', 1)[-1].startswith('ON,'):
            raise RuntimeError('Corrected AWG output enable was not confirmed')
        self.channel.verify(name, metadata, resolved)
        self.armed_record = (name, metadata, dict(resolved))
        return dict(trigger_source='EXT', waveform_name=name, correction_v=correction_v,
                    original_offset_v=original['offset_v'], offset_v=resolved['offset_v'], **receipt)

    def restore(self):
        # No software trigger, no restoration of an arbitrary previous waveform.
        self.channel.command('OUTP OFF')
        self.channel.park_dc(0.)
        if self.was_on:
            self.channel.command('OUTP ON')
        out = self.channel.query('OUTP?').split(' ', 1)[-1].split(',')[0]
        if out != ('ON' if self.was_on else 'OFF'):
            raise RuntimeError('Dedicated AWG idle output state was not restored')
        if fields(self.channel.query('BSWV?')).get('WVTP') != 'DC':
            raise RuntimeError('Dedicated AWG remains in ARB mode')
        self.changed = False

    def close(self):
        if self.changed:
            raise RuntimeError('Unverified AWG output: channel lease retained')
        self.io.close()
