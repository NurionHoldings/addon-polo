"""Map partner JSON exports into addon-polo's stable event/settlement shape.

This module deliberately does not make network requests. Provider-specific auth,
pagination, webhook signatures and status meanings must be implemented from the
partner's official documentation and tested against its sandbox.
"""
from __future__ import annotations

import datetime as dt
import argparse
import csv
import json
import re
import sys
from typing import Any, Mapping, Sequence

SALES_FIELDS = (
    "event_id", "order_id", "event_type", "date", "tenant", "amount",
    "tax_amount", "discount", "product", "payment_method", "settlement_ref",
)
SETTLEMENT_FIELDS = (
    "settlement_ref", "settlement_date", "expected_amount", "received_amount",
    "provider_fee", "bank_ref",
)
INTEGER_RE = re.compile(r"^-?\d+$")


class MappingError(ValueError):
    """Raised when a partner row cannot safely map to the canonical contract."""


def read_path(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise MappingError(f"매핑 필드를 찾을 수 없습니다: {path}")
        value = value[part]
    return value


def _mapped(record: Mapping[str, Any], mapping: Mapping[str, str], key: str, *, required: bool = True) -> Any:
    path = mapping.get(key)
    if not path:
        if required:
            raise MappingError(f"필드 매핑이 필요합니다: {key}")
        return ""
    try:
        return read_path(record, path)
    except MappingError:
        if required:
            raise
        return ""


def _integer(value: Any, name: str, *, default: int | None = None) -> int:
    if value is None or value == "":
        if default is not None:
            return default
        raise MappingError(f"{name} 값이 비어 있습니다.")
    if isinstance(value, bool):
        raise MappingError(f"{name}은 원 단위 정수여야 합니다.")
    text = str(value).replace(",", "").strip()
    if not INTEGER_RE.fullmatch(text):
        raise MappingError(f"{name}은 원 단위 정수여야 합니다: {value}")
    return int(text)


def _nonnegative_integer(value: Any, name: str, *, default: int | None = None) -> int:
    number=_integer(value,name,default=default)
    if number<0:
        raise MappingError(f"{name}은 0 이상이어야 합니다.")
    return number


def _date(value: Any, name: str) -> str:
    text = str(value or "").strip()
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise MappingError(f"{name}은 YYYY-MM-DD 또는 ISO 날짜여야 합니다: {text}") from exc


def normalize_sales(
    records: Sequence[Mapping[str, Any]],
    mapping: Mapping[str, str],
    *, tenant_name: str,
    status_map: Mapping[str, str] | None = None,
    tax_mode: str = "taxable",
) -> list[dict[str, str]]:
    """Normalize partner sale/refund rows into the current CSV import contract."""
    if tax_mode not in {"taxable", "exempt", "mixed"}:
        raise MappingError("tax_mode는 taxable/exempt/mixed 중 하나여야 합니다.")
    if not tenant_name.strip():
        raise MappingError("앱에 등록한 업체명을 지정해야 합니다.")
    statuses = {str(k).casefold(): v for k, v in (status_map or {}).items()}
    normalized: list[dict[str, str]] = []
    for index, record in enumerate(records, start=1):
        try:
            event_id = str(_mapped(record, mapping, "event_id") or "").strip()
            order_id = str(_mapped(record, mapping, "order_id") or "").strip()
            if not event_id or not order_id:
                raise MappingError("event_id와 order_id는 비워 둘 수 없습니다.")
            raw_type = str(_mapped(record, mapping, "event_type") or "").strip().casefold()
            event_type = statuses.get(raw_type, raw_type)
            event_type = {"매출": "sale", "환불": "refund", "취소": "cancel"}.get(event_type, event_type)
            if event_type not in {"sale", "refund", "cancel"}:
                raise MappingError(f"지원하지 않는 거래 상태입니다: {raw_type}")
            amount = abs(_integer(_mapped(record, mapping, "amount"), "amount"))
            tax_value = _mapped(record, mapping, "tax_amount", required=False)
            if tax_value in (None, ""):
                if tax_mode == "mixed":
                    raise MappingError("혼합과세 거래는 tax_amount 매핑과 원본 세액이 필요합니다.")
                tax = round(amount / 11) if tax_mode == "taxable" else 0
            else:
                tax = abs(_integer(tax_value, "tax_amount"))
            if tax > amount:
                raise MappingError("tax_amount가 amount보다 클 수 없습니다.")
            discount_value = _mapped(record, mapping, "discount", required=False)
            discount = abs(_integer(discount_value, "discount", default=0))
            row = {
                "event_id": event_id,
                "order_id": order_id,
                "event_type": event_type,
                "date": _date(_mapped(record, mapping, "date"), "date"),
                "tenant": tenant_name.strip(),
                "amount": str(amount),
                "tax_amount": str(tax),
                "discount": str(discount),
                "product": str(_mapped(record, mapping, "product", required=False) or ""),
                "payment_method": str(_mapped(record, mapping, "payment_method", required=False) or ""),
                "settlement_ref": str(_mapped(record, mapping, "settlement_ref", required=False) or ""),
            }
            normalized.append(row)
        except MappingError as exc:
            raise MappingError(f"{index}번째 자료: {exc}") from exc
    return normalized


def normalize_settlement(
    record: Mapping[str, Any], mapping: Mapping[str, str],
) -> dict[str, str]:
    """Normalize one provider payout/bank row into addon-polo payout CSV fields."""
    ref = str(_mapped(record, mapping, "settlement_ref") or "").strip()
    if not ref:
        raise MappingError("settlement_ref가 비어 있습니다.")
    values: dict[str, str] = {"settlement_ref": ref}
    values["settlement_date"] = _date(_mapped(record, mapping, "settlement_date"), "settlement_date")
    for key in ("expected_amount", "received_amount"):
        values[key] = str(_nonnegative_integer(_mapped(record, mapping, key), key))
    for key in ("provider_fee", "bank_ref"):
        raw = _mapped(record, mapping, key, required=False)
        values[key] = str(_nonnegative_integer(raw, key, default=0)) if key == "provider_fee" else str(raw or "")
    return values


def _record_list(document: Any, path: str | None) -> list[Mapping[str, Any]]:
    value = document
    if path:
        for part in path.split('.'):
            if not isinstance(value, Mapping) or part not in value:
                raise MappingError(f"JSON에서 목록을 찾을 수 없습니다: {path}")
            value = value[part]
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise MappingError("입력 JSON은 객체 목록이어야 합니다. 중첩 목록이면 --records-path를 지정하세요.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="파트너 JSON 자료를 addon-polo 표준 CSV로 변환")
    parser.add_argument('--kind', choices=('sales', 'settlement'), required=True)
    parser.add_argument('--input', required=True, help='업체에서 받은 JSON 표본 파일')
    parser.add_argument('--mapping', required=True, help='업체별 provider-map JSON')
    parser.add_argument('--output', required=True, help='생성할 CSV 파일 경로')
    parser.add_argument('--tenant', default='', help='매출 변환 시 프로그램에 등록된 업체명')
    parser.add_argument('--tax-mode', choices=('taxable', 'exempt', 'mixed'), default='taxable')
    parser.add_argument('--records-path', default='', help='목록이 들어 있는 JSON 경로. 예: data.orders')
    args = parser.parse_args()
    try:
        with open(args.input, encoding='utf-8') as f: source=json.load(f)
        with open(args.mapping, encoding='utf-8') as f: profile=json.load(f)
        records=_record_list(source,args.records_path or None)
        if args.kind=='sales':
            rows=normalize_sales(records,profile.get('sales_field_map',{}),tenant_name=args.tenant,status_map=profile.get('status_map',{}),tax_mode=args.tax_mode)
            fields=SALES_FIELDS
        else:
            mapping=profile.get('settlement_field_map',{})
            rows=[normalize_settlement(row,mapping) for row in records]
            fields=SETTLEMENT_FIELDS
        with open(args.output,'w',encoding='utf-8-sig',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
        print(f"{len(rows)}건을 표준 CSV로 변환했습니다: {args.output}")
    except (OSError,json.JSONDecodeError,MappingError) as exc:
        print(f"변환을 중단했습니다: {exc}",file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == '__main__':
    main()
