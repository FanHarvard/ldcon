"""Offline commissioning inventory. No DNS, sockets, services or device actions."""
import ipaddress
import math


def commissioning_issues(config,lasers):
    from ldcon.owners.dlcpro_owner import Instrument,policy_from_device,policy_check
    from ldcon.owners.dlcpro_service import make_observation
    from ldcon.core.target_policy import laser_target, target_bounds
    issues=[]
    if not config.get('host') or str(config['host']).endswith('.invalid'):issues.append('DLC hostname required')
    if not config.get('expected_serial_number') or config['expected_serial_number']=='COMMISSION_REQUIRED':issues.append('DLC serial number required')
    if not isinstance(config.get('state_directory'),str) or not config['state_directory'].startswith('/'):issues.append('Absolute owner state directory required')
    try:
        if not ipaddress.ip_address(config['listen_host']).is_loopback:raise ValueError()
        if type(config['listen_port']) is not int or not 1024<=config['listen_port']<=65535:raise ValueError()
    except (KeyError,ValueError,TypeError):issues.append('Loopback owner RPC endpoint required')
    for key in ('tick_s','socket_timeout_s'):
        value=config.get(key)
        if type(value) not in (int,float) or not math.isfinite(value) or value<=0:issues.append('Positive '+key+' required')
    try:Instrument(None,config['hardware'])
    except (ValueError,KeyError,TypeError) as error:issues.append('Physical chassis commissioning: '+str(error))
    routes=set();channels=set()
    for laser in lasers:
        tag=laser.get('id','unnamed')
        try:device=laser['devices']['dlc'];settings=device['control_settings']
        except (KeyError,TypeError):
            issues.append(tag+': DLC device definition required');continue
        if laser.get('allow_output') is not True:issues.append(tag+': output authority is disabled')
        if settings.get('commissioned') is not True:issues.append(tag+': commissioning not confirmed')
        try:
            if laser_target(laser) is None or target_bounds(laser) is None:raise ValueError('exact optical target and allowed target changes required')
        except (ValueError,TypeError) as error:issues.append(tag+': '+str(error))
        try:
            channel=device['connection']['channel']
            if type(channel) is not int or channel not in (1,2) or channel in channels:raise ValueError('Unique DLC channel 1 or 2 required')
            channels.add(channel)
            physical=config['hardware']['channels'][str(channel)]
            if physical['laser_id']!=tag:raise ValueError('Physical channel assigned to a different laser')
            if physical.get('pzt_scan_output_channel') is None:issues.append(tag+': DLC scan output selector required')
        except (ValueError,KeyError,TypeError) as error:issues.append(tag+': physical channel incomplete ('+str(error)+')')
        try:
            policy=policy_check(policy_from_device(device),config['hardware'])
            from ldcon.owners.dlcpro_ff import validate_binding
            validate_binding(policy, config['hardware']['channels'][str(device['connection']['channel'])])
            for axis in ('piezo','temperature'):
                if policy['axes'][axis]['frequency_hz_per_unit'] is None:issues.append(tag+': signed '+axis+' frequency calibration required')
        except (ValueError,KeyError,TypeError) as error:issues.append(tag+': laser policy/calibration incomplete ('+str(error)+')')
        try:
            period=settings['optical']['period_reference_px']
            if type(period) not in (int,float) or not math.isfinite(period) or period<=0:raise ValueError('Positive calibrated optical pattern period required')
        except (ValueError,KeyError,TypeError) as error:issues.append(tag+': optical calibration incomplete ('+str(error)+')')
        try:
            make_observation(device)
        except (ValueError,KeyError,TypeError) as error:issues.append(tag+': measurement route incomplete ('+str(error)+')')
        source=settings.get('frequency_source')
        if source:
            try:
                from ldcon.drivers.wsu_udp import validate_config
                udp=validate_config(source['udp'])
                route=(udp['listen_host'],udp['listen_port'])
                if route in routes:raise ValueError('Duplicate UDP listener')
                routes.add(route)
            except (ValueError,KeyError,TypeError) as error:issues.append(tag+': UDP route incomplete ('+str(error)+')')
    if not lasers:issues.append('At least one laser definition required')
    return issues
