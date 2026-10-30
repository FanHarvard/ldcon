"""Surgical presentation update: preserve live DAC conversions and other panels."""
import copy

from ldcon.web.dashboard_state import state_mappings
from ldcon.web.metric_query_profiles import SELECTOR, fleet_status_query, fleet_measurement_queries


def revise(document):
    result=copy.deepcopy(document)
    offset=fleet_measurement_queries('laser=~"${laser:regex}"')['offset']
    for panel in result['panels']:
        title=panel['title'].lower()
        if title in ('lock status','state and activity'):
            panel['title']='State and activity'
            first=panel['targets'][0]
            panel['targets']=[{**first,'expr':fleet_status_query(SELECTOR),'legendFormat':'State',
                              'instant':panel['type']=='stat','range':panel['type']!='stat'}]
            panel['fieldConfig']['defaults']['mappings']=state_mappings()
            panel['description']='Physical fault, unavailable owner and output-off take precedence. Active sweeps (preparing, armed, playback, restore), target changes, operating-point moves and studies are separate from idle lock state. A running sweep card without playback evidence is not called sweeping. Metadata expires after 25 seconds.'
        elif 'frequency offset' in title:
            panel['title']='Latest frequency offset (MHz)' if panel['type']=='stat' else 'Frequency offset from target (MHz)'
            first=panel['targets'][0]
            panel['targets']=[{**first,'expr':offset,'legendFormat':'Offset (MHz)',
                              'instant':panel['type']=='stat','range':panel['type']!='stat'}]
            panel['description']='Latest unique real wavemeter sample minus the current saved laser target at each timestamp; independent of lock state. Target comes from the laser configuration, refreshed every 5 seconds with 25-second expiry. No fixed reference, rolling median, or offset-range clipping. Missing/ambiguous source or target leaves a gap. Earlier history before target collection began remains blank.'
            defaults=panel['fieldConfig']['defaults'];defaults.update(unit='none',decimals=3)
            if panel['type']=='timeseries':defaults.setdefault('custom',{})['axisLabel']='Frequency offset (MHz)'
    result['version']=document.get('version',0)+1
    return result
