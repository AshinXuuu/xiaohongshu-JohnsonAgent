"""离线单测:KOS 可选 AI 封面(api/kos.py 的 kos_ai_cover 核心流程)。

不真调 DeepSeek / 豆包 / COS,一律 monkeypatch:
  - _fields_module    → 假 extract_fields(记录入参,返回固定三段字)
  - _cos_client/_download → 本地画一张 JPG 当作 COS 原图(顺带覆盖 ≤1200 压缩路径)
  - _cover_gen_module → 假生成模块(call_seededit 直接写回假 URL / 假错误)
造数:复制 data/usage.db 到临时目录并设 USAGE_DB_PATH,再直接落库一条测试 pack。

运行:python3 tests/test_kos_ai_cover.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 临时 DB:必须在 import lib.* 之前设好环境变量 ──
_tmpdir = tempfile.mkdtemp(prefix='kosai_test_')
_db = os.path.join(_tmpdir, 'usage.db')
shutil.copy(str(ROOT / 'data' / 'usage.db'), _db)
os.environ['USAGE_DB_PATH'] = _db

from lib import kos_store  # noqa: E402(需在设置 USAGE_DB_PATH 后导入)


def _load_kos():
    spec = importlib.util.spec_from_file_location('kos_under_test', str(ROOT / 'api' / 'kos.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _make_pack(emp):
    """直接落一条属于 emp 的 pack(不走领取主流程,本测只关心 ai_cover)。"""
    lib_id = kos_store.create_library('测试品牌', '测试产品', code='T')
    mid = kos_store.add_material(lib_id, kos_store.ROLE_MAIN, 'kos/test/cover.jpg', 'cover.jpg')
    task_id = kos_store.create_task('测试任务', '测试品牌', '测试产品', lib_id)
    copy_json = {"titles": ["第一条标题", "第二条标题"], "body": "正文内容", "tags": ["#测试"]}
    c = kos_store._conn()
    try:
        cur = c.execute(
            "INSERT INTO kos_packs(task_id,library_id,emp_id,user_name,department,post_index,"
            "cover_material_id,combo2,combo4,copy_json,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'issued',?)",
            (task_id, lib_id, emp, '测试员', '测试部', 0, mid, '[]', '[]',
             json.dumps(copy_json, ensure_ascii=False), int(time.time())))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _fake_download(client, bucket, key, dest):
    from PIL import Image
    Image.new('RGB', (1600, 2000), (200, 60, 60)).save(dest, 'JPEG')  # 宽 >1200,走压缩分支
    return dest


def _fake_cg(urls):
    def call_seededit(prompt, image_data_url, results, idx, seed):
        assert image_data_url.startswith('data:image/jpeg;base64,'), '底图必须是 JPG data url'
        assert '客厅多了它' in prompt, '三段字应注入 prompt'
        results[idx] = {'url': urls[idx]}
    return SimpleNamespace(
        map_copy_type_to_style=lambda ct: '种草氛围',
        STYLE_PROMPT_POOLS={'种草氛围': ['A {main_title}|{subtitle}|{hua_text}',
                                     'B {main_title}|{subtitle}|{hua_text}',
                                     'C {main_title}|{subtitle}|{hua_text}']},
        compose_prompt=lambda t, mt, st, ht: t.format(main_title=mt, subtitle=st, hua_text=ht),
        call_seededit=call_seededit,
        HTTP_TIMEOUT=5,
    )


def main():
    kos = _load_kos()
    emp = 'T0001'
    pack_id = _make_pack(emp)

    # ── monkeypatch:三段字 / COS / 豆包 ──
    fields_calls = []

    def fake_extract(brand, product, copy_type='', extra='',
                     existing_titles=None, existing_body='', existing_tags=None):
        fields_calls.append({'brand': brand, 'product': product,
                             'titles': list(existing_titles or []), 'body': existing_body})
        return {"main_title": "客厅多了它", "subtitle": "30天真香", "hua_text": "亲测✓"}

    kos._fields_module = lambda: SimpleNamespace(extract_fields=fake_extract)
    kos._cos_client = lambda: (None, 'bucket')
    kos._download = _fake_download
    urls = ['https://fake.doubao/1.jpg', 'https://fake.doubao/2.jpg', 'https://fake.doubao/3.jpg']
    kos._cover_gen_module = lambda: _fake_cg(urls)
    os.environ['KOS_AI_COVER_N'] = '2'

    # 1) 归属校验:pack 不存在 → 404;别人的 pack → 403(且不该走到限流/生成)
    code, obj = kos.kos_ai_cover(999999, emp)
    assert code == 404, (code, obj)
    code, obj = kos.kos_ai_cover(pack_id, 'E9999',
                                 rl_check=lambda: (_ for _ in ()).throw(AssertionError('403 不应触发限流')))
    assert code == 403 and '无权' in obj['error'], (code, obj)

    # 2) 限流:rl_check 拒绝 → 429 透传文案
    code, obj = kos.kos_ai_cover(pack_id, emp, rl_check=lambda: (False, '今日次数已达上限'))
    assert code == 429 and obj['error'] == '今日次数已达上限', (code, obj)

    # 3) 正常返回结构:ok / covers(N=2)/ fields 三段字
    code, obj = kos.kos_ai_cover(pack_id, emp, rl_check=lambda: (True, None))
    assert code == 200, (code, obj)
    assert obj['ok'] is True and obj['covers'] == urls[:2], obj
    assert obj['fields'] == {"main_title": "客厅多了它", "subtitle": "30天真香", "hua_text": "亲测✓"}
    # 三段字提炼入参:brand/product 来自 task,标题只取第一条,正文带上
    assert fields_calls[-1] == {'brand': '测试品牌', 'product': '测试产品',
                                'titles': ['第一条标题'], 'body': '正文内容'}, fields_calls

    # 4) 全部生成失败 → 502 + 约定文案
    cg = _fake_cg(urls)
    def fail_seededit(prompt, image_data_url, results, idx, seed):
        results[idx] = {'error': 'boom'}
    cg.call_seededit = fail_seededit
    kos._cover_gen_module = lambda: cg
    code, obj = kos.kos_ai_cover(pack_id, emp)
    assert code == 502 and obj['error'] == 'AI 封面生成失败,请稍后重试(不影响已领取的图文)', (code, obj)

    print('OK - kos ai_cover 单测 4 组断言全部通过')


if __name__ == '__main__':
    try:
        main()
    finally:
        shutil.rmtree(_tmpdir, ignore_errors=True)
