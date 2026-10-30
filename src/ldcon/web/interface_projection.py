"""Compatibility view only: storage keeps lasers, decks and assignment separate."""
from ldcon.core.interface_schema import digest


def project_assignment(bundle):
    laser=bundle['laser_definition']['document'];decks=[]
    for value in bundle['deck_definitions']:
        definition=value['document'];statuses=value['card_status'];first=definition['cards'][0]
        p=statuses[first['id']]['parameters'];cards=[]
        for card in definition['cards']:
            cards.append({'id':card['id'],'card':card['operation'],'params':statuses[card['id']]['parameters'],
                          'uses':card['uses'],'bindings':{name:'interface:'+name for name in card['uses']}})
        decks.append({'schema_version':1,'id':definition['id'],'name':definition['name'],
            'description_ja':definition['name'],'mode':'finite','duration_s':p.get('duration_s',p.get('timeout_s')),
            'sample_s':p.get('sample_interval_s',.5),'recovery_budget':0,'cards':cards,'safety':{'output_limits':{},'expressions':[],'max_variable_age_s':p.get('max_sample_age_s',1.)},
            'execution':{'backend':'interface-live'},'card_status':statuses,'runnable':statuses[first['id']]['runnable']})
    identity={'id':laser['id'],'name':laser['name'],'kind':'interface-laser'}
    document={'schema_version':1,'id':bundle['id'],'name':laser['name'],
              'description':'レーザー機器定義と共通インターフェースデッキを別々に管理します。',
              'laser':identity,'default_deck':decks[0]['id'],'decks':decks}
    ready=any(deck['runnable'] for deck in decks)
    issues=[reason for d in decks for s in d['card_status'].values() for reason in s['blocked_reasons']]
    return {**bundle,'source_document':bundle['document'],'document':document,
            'storage_kind':'interface-assignment','revision':bundle['definition_revision'],
            'assignment_revision':bundle['revision'],'runnable':ready,
            'blocked_reason':' / '.join(dict.fromkeys(issues)),
            'deck_revisions':{d['id']:digest(d) for d in decks}}
