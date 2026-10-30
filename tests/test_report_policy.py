"""Report policy reaches existing custom sessions as well as new task bundles."""
import pytest
from ldcon.agents import agent_bundle, agent_workspace
from ldcon.agents import report_pdf


def test_old_custom_template_cannot_keep_obsolete_html_report_policy(tmp_path):
    common=agent_workspace.communication_instructions(dict(bridge_id='laser-test',teams_card_ids='none'))
    assert 'LuaLaTeX PDF' in common and 'superseding old HTML-report instructions' in common
    assert 'CERN ROOT and roothelper' in common and 'DataSaver' in common
    old='User notes: preserve this section.\n'
    managed=agent_workspace._managed_document(old,common)
    assert managed.startswith(old) and 'LuaLaTeX PDF' in managed
    assert 'preserve' in common.lower() or 'historical HTML records intact' in common


def test_task_bundle_pdf_policy(tmp_path):
    bundle=agent_bundle.build(tmp_path,'adjustment',context=dict(laser_id='laser'),evidence={})
    assert bundle['instruction_revision']==agent_bundle.REVISION
    assert 'LuaLaTeX PDF' in bundle['instructions']
    assert 'HTML report.' not in bundle['instructions']


def test_template_is_editable_and_does_not_overwrite(tmp_path):
    folder=report_pdf.initialize(tmp_path/'report')
    assert '\\includegraphics' in (folder/'ldconreport.cls').read_text()
    assert 'LuaLaTeX' in (folder/'ldconreport.cls').read_text()
    assert '\\ReportFigure{figures/response.pdf}' in (folder/'report.tex').read_text()
    assert 'ROOT + roothelper' in (folder/'report.tex').read_text()
    (folder/'report.tex').write_text('existing report')
    with pytest.raises(ValueError,match='not overwritten'):report_pdf.initialize(folder)
    assert (folder/'report.tex').read_text()=='existing report'


def test_scoped_text_tool_accepts_report_sources_only(tmp_path):
    from ldcon.core.laser_assets import LaserAssets, relative_path, AssetConflict
    assets=LaserAssets(tmp_path,'laser')
    for suffix in ('.tex','.cls','.sty','.bib'):
        path='reports/study/source'+suffix
        result=assets.write(path,'Reviewed report source','')
        assert assets.read(path)['content']=='Reviewed report source'
        with pytest.raises(AssetConflict):assets.write(path,'different','')
        assert result['sha256']
        with pytest.raises(ValueError):relative_path('cards/source'+suffix)
    with pytest.raises(ValueError):relative_path('reports/source.pdf')
    with pytest.raises(ValueError):relative_path('reports/../secrets/source.tex')
