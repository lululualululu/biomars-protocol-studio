"""Import the owner's 50 generated drafts; exclude PDFs, logs and private paths.

Usage: python scripts/import_examples.py /path/to/50题修订版汇总.json
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
# Concise browsing labels; the original query and draft remain unchanged.
LABELS = [
 ('O01', '肾脏类器官', 'Kidney organoids', 'organoids'),
 ('O02', '迭代免疫荧光', 'Iterative immunofluorescence', 'imaging'),
 ('O03', '中性粒细胞分离', 'Neutrophil isolation', 'cells'),
 ('O04', '巨噬细胞极化', 'Macrophage polarization', 'cells'),
 ('O05', '皮层类器官', 'Cortical organoids', 'organoids'),
 ('O06', '滋养层类器官', 'Trophoblast organoids', 'organoids'),
 ('O07', '细胞迁移划痕实验', 'Cell migration scratch assay', 'assays'),
 ('O08', '患者来源肿瘤类器官', 'Patient-derived tumor organoids', 'organoids'),
 ('O09', '气道类器官', 'Airway organoids', 'organoids'),
 ('O10', '视网膜细胞分化', 'Retinal differentiation', 'cells'),
 ('V2-01', 'iPSC 小胶质细胞分化', 'iPSC-derived microglia', 'cells'),
 ('V2-02', '红细胞分化', 'Erythroid differentiation', 'cells'),
 ('V2-03', '骨骼肌祖细胞分化', 'Skeletal myogenic progenitors', 'cells'),
 ('V2-04', '外周感觉神经元', 'Peripheral sensory neurons', 'cells'),
 ('V2-05', '心外膜细胞分化', 'Epicardial differentiation', 'cells'),
 ('V2-06', '子宫内膜类器官', 'Endometrial organoids', 'organoids'),
 ('V2-07', '输卵管类器官', 'Fallopian tube organoids', 'organoids'),
 ('V2-08', '乳腺类器官', 'Mammary organoids', 'organoids'),
 ('V2-09', '尿液来源肾小管类器官', 'Urine-derived tubuloids', 'organoids'),
 ('V2-10', '成人肝脏类器官', 'Adult liver organoids', 'organoids'),
 ('V2-11', '小鼠原代肝细胞', 'Primary mouse hepatocytes', 'cells'),
 ('V2-12', '小鼠胰岛分离', 'Mouse islet isolation', 'cells'),
 ('V2-13', '成年小鼠心肌细胞', 'Adult mouse cardiomyocytes', 'cells'),
 ('V2-14', '骨骼肌卫星细胞', 'Skeletal muscle satellite cells', 'cells'),
 ('V2-15', '间充质基质细胞培养', 'Mesenchymal stromal cells', 'cells'),
 ('V2-16', '嗜酸性粒细胞分离', 'Eosinophil isolation', 'cells'),
 ('V2-17', '肿瘤浸润淋巴细胞初代培养', 'Primary infiltrating lymphocytes', 'cells'),
 ('V2-18', '调节性 T 细胞诱导', 'Regulatory T-cell induction', 'cells'),
 ('V2-19', 'Th17 细胞分化', 'Th17 differentiation', 'cells'),
 ('V2-20', '星形胶质细胞分化', 'Astroglial differentiation', 'cells'),
 ('V2-21', '膨胀显微与 FISH', 'Expansion microscopy & FISH', 'imaging'),
 ('V2-22', '全脑透明化成像', 'Whole-brain clearing & imaging', 'imaging'),
 ('V2-23', '单分子 RNA FISH', 'Single-molecule RNA FISH', 'imaging'),
 ('V2-24', '活细胞钙成像', 'Live-cell calcium imaging', 'imaging'),
 ('V2-25', '原位邻近连接检测', 'In situ proximity ligation', 'assays'),
 ('V2-26', '线粒体压力测试', 'Mitochondrial stress test', 'assays'),
 ('V2-27', '流式细胞凋亡检测', 'Flow-cytometric apoptosis assay', 'assays'),
 ('V2-28', '细胞内活性氧检测', 'Intracellular ROS imaging', 'imaging'),
 ('V2-29', '斑马鱼脑细胞核分离', 'Zebrafish brain nuclei isolation', 'cells'),
 ('V2-30', '巨噬细胞胞葬检测', 'Macrophage efferocytosis', 'assays'),
 ('V2-31', 'iPSC CRISPR 编辑', 'iPSC CRISPR editing', 'engineering'),
 ('V2-32', '原代 T 细胞 CRISPR 编辑', 'Primary T-cell CRISPR editing', 'engineering'),
 ('V2-33', '原代 T 细胞标记转导', 'Primary T-cell marker transduction', 'engineering'),
 ('V2-34', 'iPSC PiggyBac 工程', 'iPSC PiggyBac engineering', 'engineering'),
 ('V2-35', '皮层类器官电转', 'Cortical organoid electroporation', 'engineering'),
 ('V2-36', 'Caco-2 屏障完整性', 'Caco-2 barrier integrity', 'assays'),
 ('V2-37', '悬滴法肿瘤球', 'Hanging-drop tumor spheroids', 'organoids'),
 ('V2-38', '内皮细胞成管实验', 'Endothelial tube formation', 'assays'),
 ('V2-39', 'T 细胞激活与扩增', 'T-cell activation & expansion', 'cells'),
 ('V2-40', '胰岛微囊化', 'Islet microencapsulation', 'assays'),
]


def import_examples(manifest):
    data = json.loads(manifest.read_text())
    rows = {row['query_id']: row for row in data['rows']}
    assert len(rows) == 50 and set(rows) == {x[0] for x in LABELS}
    destination = ROOT / 'web/examples'
    destination.mkdir(exist_ok=True)
    index = []
    for case_id, zh, en, category in LABELS:
        row = rows[case_id]
        raw = Path(row['delivery_protocol']).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        assert digest == row['protocol_sha256'], case_id
        markdown = raw.decode('utf-8')
        assert not any(marker in markdown for marker in ('/Users/', 'file://', 'sk-or-v1-')), case_id
        metadata = {'id': case_id, 'title_zh': zh, 'title_en': en, 'category': category,
                    'query': row['query'], 'model': row['model'], 'doi': row['doi']}
        index.append(metadata)
        notes = ['Historical model-generated draft. Review the source and applicability before use.']
        notes.append('Recorded source match: ' + row.get('match_status', 'not recorded'))
        if case_id in data.get('new_native_integrity_validation_not_passed', []):
            notes.append('The recorded integrity check did not pass; this case requires additional review.')
        if case_id in data.get('new_native_pdf_identity_unavailable', []):
            notes.append('The recorded PDF identity check was unavailable.')
        result = {**metadata, 'title': row['paper_title'] or en, 'state': 'example', 'historical': True,
                  'markdown': markdown, 'review_notes': notes,
                  'sources': [{'title': row['paper_title'], 'doi': row['doi'],
                               'url': ('https://doi.org/' + quote(row['doi'], safe='/')) if row.get('doi') else None,
                               'source': 'Primary reference · 原始案例主文献'}],
                  'provenance': {'dataset': data['dataset_version'], 'date': data['updated_at'][:10],
                                 'protocol_sha256': digest, 'match_status': row.get('match_status')}}
        (destination / f'{case_id}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    (destination / 'index.json').write_text(json.dumps({'count': 50, 'featured': ['O07','O02','O01'],
        'model': data['model'], 'date': data['updated_at'][:10], 'cases': index}, ensure_ascii=False, indent=2)+'\n')
    print('Imported 50 drafts; all original SHA-256 hashes verified. No source PDFs or logs copied.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    import_examples(parser.parse_args().manifest)
