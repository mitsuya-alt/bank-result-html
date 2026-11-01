"""API Gateway → Lambda → Aurora → Markdown → HTML。"""
import argparse
import html
import json
import logging
import os
import re
import sys
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo

import markdown
import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SQL = """
SELECT
    br.bank_result_id,
    br.invoice_id,
    br.result_code,
    br.amount,
    br.received_at,
    tm.markdown_body
FROM public.bank_result AS br
INNER JOIN public.template_master AS tm
    ON br.template_id = tm.template_id
WHERE br.bank_result_id = %s
"""


class ResultNotFoundError(Exception):
    """入金結果、または対応するテンプレートが見つからない。"""


def get_connection():
    """SAMから渡された環境変数を優先し、未設定ならローカル設定を使う。"""
    # ローカル設定の初期値は例です。実際のDBeaverの接続設定に合わせてください。
    db_url = os.environ.get(
        "DB_URL",
        "host=localhost port=5432 dbname=insurance_db sslmode=disable",
    )
    db_username = os.environ.get("DB_USERNAME", "postgres")
    db_password = os.environ.get("DB_PASSWORD", "postgres")

    return psycopg.connect(
        db_url,
        user=db_username,
        password=db_password,
        connect_timeout=5,
        options="-c statement_timeout=5000 -c default_transaction_read_only=on",
        row_factory=dict_row,
    )


def fetch_bank_result(bank_result_id):
    """Lambda・mainで共通の、実DBからの取得処理。"""
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(SQL, (bank_result_id,))
            row = cursor.fetchone()
    if row is None:
        raise ResultNotFoundError("入金結果またはテンプレートがありません。")
    return row


def escape_value(value):
    """埋め込むデータを、HTMLやMarkdownの構文として解釈させない。"""
    escaped = html.escape(str(value), quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", escaped)


def render_html(row):
    """取得した1行を使い、クライアントに返せるHTML断片を作る。"""
    received_at = row["received_at"]
    if received_at is not None:
        if received_at.tzinfo is not None:
            received_at = received_at.astimezone(ZoneInfo("Asia/Tokyo"))
        received_at = received_at.isoformat(sep=" ", timespec="seconds")
    else:
        received_at = "未設定"

    values = {
        "bank_result_id": row["bank_result_id"],
        "invoice_id": row["invoice_id"],
        "result_code": row["result_code"] if row["result_code"] is not None else "未設定",
        "amount": format(row["amount"], ",f") if row["amount"] is not None else "未設定",
        "received_at": received_at,
    }
    values = {key: escape_value(value) for key, value in values.items()}

    # substituteは、変数名の誤り・未定義変数をエラーにする。
    filled_markdown = Template(row["markdown_body"]).substitute(values)
    return markdown.markdown(filled_markdown)


def create_document(fragment):
    """UTF-8を明示した、返却用のHTML文書で包む。"""
    return (
        '<!doctype html>\n<html lang="ja">\n<head>\n'
        '<meta charset="utf-8">\n<title>入金結果</title>\n'
        '</head>\n<body>\n' + fragment + '\n</body>\n</html>\n'
    )


def response(status, body, content_type="text/html; charset=utf-8"):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": content_type,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
        "isBase64Encoded": False,
        "body": body,
    }


def error_response(status, message):
    return response(status, json.dumps({"message": message}, ensure_ascii=False),
                    "application/json; charset=utf-8")


def lambda_handler(event, context):
    """GET /bank-results/{bank_result_id}/html のLambdaプロキシ統合。"""
    # Lambdaコンソールのテストでも同じpathParameters形式を使用。
    parameters = event.get("pathParameters") if isinstance(event, dict) else None
    raw_id = parameters.get("bank_result_id") if isinstance(parameters, dict) else None
    if not isinstance(raw_id, str) or not re.fullmatch(r"[0-9]{1,19}", raw_id):
        return error_response(400, "bank_result_idは正の整数で指定してください。")
    bank_result_id = int(raw_id)
    if not 1 <= bank_result_id <= 9223372036854775807:
        return error_response(400, "bank_result_idが有効範囲外です。")

    try:
        row = fetch_bank_result(bank_result_id)
        return response(200, create_document(render_html(row)))
    except ResultNotFoundError:
        return error_response(404, "入金結果またはテンプレートがありません。")
    except Exception as exc:
        # 接続情報・Secret・SQL・データ本文はログに出さない。
        request_id = getattr(context, "aws_request_id", "local")
        logger.error("HTML generation failed: type=%s request_id=%s",
                     type(exc).__name__, request_id)
        return error_response(500, "入金結果のHTML生成に失敗しました。")


def main(argv=None):
    """実DBへ接続し、Lambdaと同じ処理を実行してHTMLを保存する。"""
    parser = argparse.ArgumentParser(description="入金結果をDBから取得してHTMLを生成")
    parser.add_argument("bank_result_id", help="例: 5")
    parser.add_argument("--output", default="bank_result.html", help="出力先HTML")
    args = parser.parse_args(argv)
    event = {"pathParameters": {"bank_result_id": args.bank_result_id}}
    result = lambda_handler(event, None)
    print(f"statusCode: {result['statusCode']}")
    if result["statusCode"] != 200:
        print(result["body"], file=sys.stderr)
        return 1
    try:
        output = Path(args.output)
        output.write_text(result["body"], encoding="utf-8")
    except OSError:
        print("出力先と書き込み権限を確認してください。", file=sys.stderr)
        return 1
    print(f"HTML: {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
