"""Local-owner setup. Run with the customer site up and the assistant stopped."""
import json

from app import Runtime, default_data_dir
from mock_shop.app import default_folder
from security import decrypt_secret, encrypt_secret, load_master_key
from shop_connector import ShopBoard


def link():
    folder=default_folder()
    path=folder/'integration.qkey'
    if not path.exists():
        raise SystemExit('먼저 start-shop.bat로 고객사이트를 실행해 주세요.')
    credentials=json.loads(decrypt_secret(path.read_text(encoding='utf-8'),load_master_key(folder)))
    runtime=Runtime(default_data_dir())
    url='http://127.0.0.1:8766'
    board=ShopBoard(url,credentials['token'])
    board.list_questions()
    board.request('PUT','/integration/webhook',{'url':'http://127.0.0.1:8765/api/webhook/board','secret':runtime.webhook_secret()})
    settings=runtime.engine.settings()
    changed=settings.get('mode')!='shop' or settings.get('shop_url')!=url
    runtime.internal_settings({'mode':'shop','shop_url':url,'main_url':url,'qna_url':url,
                               'shop_token_encrypted':encrypt_secret(credentials['token'],runtime.key),
                               'auto_enabled':False,'evaluation_passed':False,
                               **({'baseline_complete':False,'evaluation':None} if changed else {})})
    runtime.configure_board()
    result=runtime.engine.sync('local-setup')
    print(json.dumps({'connected':True,'newly_imported_questions':result['added'],'customer_url':url},ensure_ascii=True))


if __name__=='__main__':
    link()
