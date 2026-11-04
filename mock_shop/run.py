import argparse
import threading
import webbrowser

import uvicorn

from mock_shop.app import create_app, default_folder


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description='MORU 모의 고객사이트')
    parser.add_argument('--port',type=int,default=8766)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    if not args.no_browser:
        threading.Timer(1.2,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    uvicorn.run(create_app(default_folder()),host='127.0.0.1',port=args.port,access_log=False)
