"""Resolve the newest active DLC target, including temporary study targets."""
from ldcon.web.metric_query_profiles import SELECTOR, configured_target_query


def effective_target_query():
    # The worker publishes faster than independent monitoring. Choose the
    # newest publication, not a fixed producer priority or the largest target.
    scope = SELECTOR+',kind=~"trace|owner_sample"'
    observed = 'ldcon_observed_s{'+scope+'}'
    stamp = 'tlast_over_time('+observed+'[25s])'
    newest = '('+stamp+' == on(laser) group_left max by(laser) ('+stamp+'))'
    def field(name):
        metric = 'ldcon_'+name+'{'+scope+'}'
        return ('(last_over_time('+metric+'[25s]) and '
                '(tlast_over_time('+metric+'[25s]) == '+stamp+') and '+newest+')')
    target = field('target_frequency_hz')
    active = field('open_loop')+' == 0'
    valid = '('+target+' and ('+active+'))'
    unique = '(max by(laser) ('+valid+') and on(laser) (count by(laser) ('+valid+') == 1))'
    configured = configured_target_query(SELECTOR)
    # An active but incomplete newest snapshot cannot resurrect a saved target.
    live = 'group by(laser) ('+active+')'
    return '('+unique+' or on(laser) ('+configured+' unless on(laser) '+live+'))'
