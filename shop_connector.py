"""Authenticated HTTP connection to the separate local customer site."""
import re

import httpx

from mock_shop.app import local_url


class ShopBoard:
    def __init__(self, url, token, transport=None):
        self.url=local_url(url)
        self.token=token
        self.transport=transport

    def request(self, method, path, data=None):
        try:
            with httpx.Client(base_url=self.url,headers={'Authorization':'Bearer '+self.token},
                              timeout=8,trust_env=False,follow_redirects=False,transport=self.transport) as client:
                response=client.request(method,path,json=data)
            if response.status_code==404:
                return None
            if response.status_code==401:
                raise ValueError('모의 고객사이트 연결 인증에 실패했습니다.')
            if response.status_code==409:
                raise ValueError('고객사이트의 문의가 변경되었거나 이미 답글이 있습니다. 게시 결과를 확인해 주세요.')
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError,ValueError) as exc:
            if isinstance(exc,ValueError) and not isinstance(exc,httpx.HTTPError):
                raise
            raise ValueError('모의 고객사이트 응답을 확인할 수 없습니다. 서버 실행 상태를 확인해 주세요.') from None

    @staticmethod
    def validate(row):
        if not isinstance(row,dict) or not re.fullmatch(r'shop-[1-9][0-9]*',str(row.get('id',''))):
            raise ValueError('고객사이트 문의 형식이 올바르지 않습니다.')
        if any(not isinstance(row.get(k),str) for k in ('title','body','category','answer','created_at')) or not isinstance(row.get('version'),int):
            raise ValueError('고객사이트 문의 필드가 올바르지 않습니다.')
        return row

    def list_questions(self):
        rows=self.request('GET','/integration/questions')
        if not isinstance(rows,list) or len(rows)>10000:
            raise ValueError('고객사이트 문의 목록을 확인할 수 없습니다.')
        return [self.validate(r) for r in rows]

    def question(self, external_id):
        if not re.fullmatch(r'shop-[1-9][0-9]*',str(external_id)):
            return None
        row=self.request('GET','/integration/questions/'+external_id)
        return self.validate(row) if row else None

    def answer(self, external_id, text, operation_key, version):
        row=self.request('POST','/integration/questions/'+external_id+'/answer',
                         {'answer':text,'operation_key':operation_key,'expected_version':version})
        return self.validate(row)
