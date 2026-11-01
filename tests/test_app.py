"""正常系・異常系・境界値を確認する。DB取得・接続はモック化しない。"""
import pytest
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import runpy
import sys
import app

@contextmanager
def environment(**values):
    """設定値だけを一時的に変更し、終了時に復元する。処理の置換はしない。"""
    old = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

def sample_row(**overrides):
    """変換関数単体へ渡す入力データ。DB関数の戻り値の置換には使わない。"""
    row = {'bank_result_id': 5, 'invoice_id': 14, 'result_code': 'SUCCESS', 'amount': Decimal('10000.00'), 'received_at': datetime(2026, 10, 12, 20, 34, 22, tzinfo=timezone.utc), 'markdown_body': '## 入金結果\n\n- ID：${bank_result_id}\n- 請求：${invoice_id}\n- 結果：${result_code}\n- 金額：${amount}円\n- 日時：${received_at}'}
    row.update(overrides)
    return row

class TestFunctions:
    """入力検証と変換処理。この範囲にはDBアクセス自体がない。"""

    def test_invalid_inputs(self):
        for value in (None, '', '0', '-1', 'abc', '1 OR 1=1', 5, True, '1.5', '9' * 20, '9223372036854775808'):
            result = app.lambda_handler({'pathParameters': {'bank_result_id': value}}, None)
            assert result['statusCode'] == 400
            assert 'message' in json.loads(result['body'])
        for event in (None, {}, {'pathParameters': None}, {'pathParameters': []}):
            assert app.lambda_handler(event, None)['statusCode'] == 400

    def test_escape(self):
        for source, expected in (('**OK**', '\\*\\*OK\\*\\*'), ('a_b', 'a\\_b'), ('[x](url)', '\\[x\\]\\(url\\)'), ('a\\b', 'a\\\\b')):
            assert app.escape_value(source) == expected
        assert app.escape_value(123) == '123'
        result = app.render_html(sample_row(result_code='<script>alert(1)</script> **bold** [x](url)'))
        assert '&lt;script&gt;' in result
        for tag in ('<script>', '<strong>', '<a '):
            assert tag not in result

    def test_format_and_timezone(self):
        result = app.render_html(sample_row())
        assert '<h2>入金結果</h2>' in result
        assert '10,000.00円' in result
        assert '2026-10-13 05:34:22+09:00' in result
        assert '${' not in result

    def test_naive_datetime(self):
        result = app.render_html(sample_row(received_at=datetime(2026, 10, 13, 5, 34, 22)))
        assert '2026-10-13 05:34:22' in result
        assert '+09:00' not in result

    def test_null_values(self):
        result = app.render_html(sample_row(received_at=None, amount=None, result_code=None))
        assert result.count('未設定') == 3

    def test_zero_and_negative_amount(self):
        for amount, expected in ((Decimal('0'), '0円'), (Decimal('-1'), '-1円')):
            assert expected in app.render_html(sample_row(amount=amount))

    def test_invalid_template(self):
        with pytest.raises(KeyError):
            app.render_html(sample_row(markdown_body='${unknown_name}'))
        with pytest.raises(ValueError):
            app.render_html(sample_row(markdown_body='${'))

    def test_document_and_responses(self):
        document = app.create_document('<p>本文</p>')
        assert '<meta charset="utf-8">' in document
        assert '<body>\n<p>本文</p>\n</body>' in document
        assert app.response(200, document)['body'] == document
        error = app.error_response(400, '入力不正')
        assert error['statusCode'] == 400
        assert json.loads(error['body']) == {'message': '入力不正'}
        assert error['headers']['Content-Type'] == 'application/json; charset=utf-8'

    def test_main_invalid_id(self):
        assert app.main(['abc']) == 1

    def test_main_entry_point(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [app.__file__, "abc"])

        with pytest.raises(SystemExit) as exc_info:
            runpy.run_path(app.__file__, run_name="__main__")

        assert exc_info.value.code == 1
        assert "statusCode: 400" in capsys.readouterr().out

@pytest.mark.db
class TestDatabase:
    """ローカルはローカルDB、CodeBuildは環境変数で指定したAWSのDB。"""

    @pytest.fixture(scope='class', autouse=True)
    def setup_database(self, request):
        cls = request.cls
        cls.valid_id = int(os.environ.get('TEST_BANK_RESULT_ID', '5'))
        try:
            cls.row = app.fetch_bank_result(cls.valid_id)
            with app.get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT COALESCE(MAX(bank_result_id), 0) AS max_id FROM public.bank_result')
                    cls.missing_id = cursor.fetchone()['max_id'] + 1
        except Exception:
            raise RuntimeError('実DBの接続・取得に失敗しました。DB設定、権限、ネットワークとテストデータを確認してください。') from None
        if cls.missing_id > 9223372036854775807:
            raise RuntimeError('不存在IDのテスト用に、上限未満のIDを使うDBを用意してください。')

    def test_database_connection(self):
        with app.get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1 AS ok')
                assert cursor.fetchone()['ok'] == 1

    def test_real_fetch(self):
        assert self.row['bank_result_id'] == self.valid_id
        assert self.row['markdown_body'].strip()

    def test_handler_success(self):
        result = app.lambda_handler({'pathParameters': {'bank_result_id': str(self.valid_id)}}, None)
        assert result['statusCode'] == 200
        assert result['headers']['Content-Type'] == 'text/html; charset=utf-8'
        assert '<!doctype html>' in result['body']
        assert '${' not in result['body']

    def test_real_missing_row(self):
        with pytest.raises(app.ResultNotFoundError):
            app.fetch_bank_result(self.missing_id)
        result = app.lambda_handler({'pathParameters': {'bank_result_id': str(self.missing_id)}}, None)
        assert result['statusCode'] == 404

    def test_real_authentication_failure(self):
        invalid_user = 'no_such_test_user_' + uuid.uuid4().hex
        with environment(DB_USERNAME=invalid_user):
            with pytest.raises(app.psycopg.Error):
                with app.get_connection():
                    pass
            result = app.lambda_handler({'pathParameters': {'bank_result_id': str(self.valid_id)}}, None)
        assert result['statusCode'] == 500
        assert invalid_user not in result['body']

    def test_main_success_and_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'result.html'
            assert app.main([str(self.valid_id), '--output', str(output)]) == 0
            assert '<!doctype html>' in output.read_text(encoding='utf-8')
            missing_output = Path(directory) / 'missing.html'
            assert app.main([str(self.missing_id), '--output', str(missing_output)]) == 1
            assert not missing_output.exists()

    def test_main_output_error(self):
        with tempfile.TemporaryDirectory() as directory:
            assert app.main([str(self.valid_id), '--output', directory]) == 1
