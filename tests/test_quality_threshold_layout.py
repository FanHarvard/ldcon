import json
from types import SimpleNamespace

import pytest
import yaml
from ldcon.web.quality_threshold_metrics import collect
from ldcon.core.state_layout import SCHEMA


@pytest.mark.parametrize('canonical',[False,True])
def test_thresholds_follow_layout_marker_and_saved_revisions(tmp_path,canonical):
    (tmp_path/'jobs').mkdir()
    service=SimpleNamespace(state_file=tmp_path/'web-server.json')
    if canonical:(tmp_path/'laser-layout.json').write_text(json.dumps(dict(schema=SCHEMA,laser_id='laser')))
    root=tmp_path/'config' if canonical else tmp_path
    path=root/'lasers/laser.yaml';path.parent.mkdir(parents=True)
    def write(value):
        policy=dict(classification_enabled=True,single_mode_max=dict(side_peak_height_max=value),warning_max=dict(side_peak_height_max=.4))
        path.write_text(yaml.safe_dump(dict(id='laser',devices=dict(rig=dict(control_settings=dict(optical=policy))))))
    write(.31)
    assert collect(service,'laser')['quality_single_mode_max_side_peak_height_max']==.31
    write(.32)
    result=collect(service,'laser')
    assert result['quality_thresholds_read_ok']==1
    assert result['quality_single_mode_max_side_peak_height_max']==.32
    assert not any(k.startswith('quality_warning_max_') for k in result)


def test_missing_or_symlink_configuration_never_invents_thresholds(tmp_path):
    (tmp_path/'jobs').mkdir();(tmp_path/'lasers').mkdir()
    service=SimpleNamespace(state_file=tmp_path/'web-server.json')
    assert collect(service,'laser')==dict(quality_thresholds_read_ok=0)
    path=tmp_path/'real.yaml';path.write_text('id: laser\ndevices: {}\n')
    (tmp_path/'lasers/laser.yaml').symlink_to(path)
    assert collect(service,'laser')==dict(quality_thresholds_read_ok=0)
