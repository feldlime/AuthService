import re
import typing as tp
from datetime import datetime, timedelta
from http import HTTPStatus
from uuid import UUID

import orjson
import pytest
from sqlalchemy import orm
from starlette.testclient import TestClient

from auth_service.db.models import (
    Base,
    NewcomerTable,
    RegistrationTokenTable,
    UserTable,
)
from auth_service.mail.config import (
    REGISTRATION_EMAIL_SENDER,
    REGISTRATION_EMAIL_SUBJECT,
)
from auth_service.security import SecurityService
from auth_service.settings import ServiceConfig
from auth_service.utils import utc_now
from tests.constants import (
    REGISTER_VERIFY_LINK_TEMPLATE,
    USER_EMAIL,
    USER_NAME,
    USER_PASSWORD,
)
from tests.helpers import (
    FakeMailgunServer,
    assert_all_tables_are_empty,
    make_db_user,
)
from tests.utils import ApproxDatetime

REGISTRATION_PATH = "/auth/register"

REGISTER_REQUEST_BODY = {
    "name": USER_NAME,
    "email": USER_EMAIL,
    "password": USER_PASSWORD,
}


def test_registration_success(
    client: TestClient,
    db_session: orm.Session,
    security_service: SecurityService,
    service_config: ServiceConfig,
    fake_mailgun_server: FakeMailgunServer,
):
    request_body = REGISTER_REQUEST_BODY.copy()
    now = utc_now()
    with client:
        resp = client.post(
            REGISTRATION_PATH,
            json=request_body,
        )

    # Check response
    assert resp.status_code == HTTPStatus.CREATED
    resp_json = resp.json()
    assert set(resp_json.keys()) == {"user_id", "name", "email", "created_at"}
    assert UUID(resp_json["user_id"]).version == 4
    assert resp_json["name"] == request_body["name"]
    assert resp_json["email"] == request_body["email"]
    assert (
        datetime.fromisoformat(resp_json["created_at"])
        == ApproxDatetime(now)
    )

    # Check DB content
    assert_all_tables_are_empty(
        db_session,
        [NewcomerTable, RegistrationTokenTable],
    )
    newcomers = db_session.query(NewcomerTable).all()
    reg_tokens = db_session.query(RegistrationTokenTable).all()

    assert len(newcomers) == 1
    assert len(reg_tokens) == 1
    newcomer = newcomers[0]
    reg_token = reg_tokens[0]

    newcomer_dict = orjson.loads(
        orjson.dumps({k: getattr(newcomer, k) for k in resp_json.keys()})
    )
    for field, value in resp_json.items():
        assert newcomer_dict[field] == value
    assert security_service.is_password_correct(
        request_body["password"],
        newcomer.password
    )

    assert reg_token.user_id == newcomer.user_id
    assert reg_token.created_at == ApproxDatetime(now)
    assert reg_token.expired_at == ApproxDatetime(
        now
        + timedelta(
            seconds=service_config
            .security_config
            .registration_token_lifetime_seconds,
        )
    )

    # Check sent mail
    assert len(fake_mailgun_server.requests) == 1
    send_mail_request = fake_mailgun_server.requests[0]
    assert send_mail_request.authorization == {
        "username": "api",
        "password": service_config.mail_config.mailgun_config.mailgun_api_key,
    }
    assert (
        send_mail_request.form["from"]
        == REGISTRATION_EMAIL_SENDER.format(
            domain=service_config.mail_config.mail_domain
        )
    )
    assert send_mail_request.form["to"] == newcomer.email
    assert send_mail_request.form["subject"] == REGISTRATION_EMAIL_SUBJECT

    link_pattern = (
        REGISTER_VERIFY_LINK_TEMPLATE
        .replace("{token}", r"(\w+)")
        .replace("?", r"\?")
    )
    text_token = re.findall(link_pattern, send_mail_request.form["text"])[0]
    html_token = re.findall(link_pattern, send_mail_request.form["html"])[0]
    assert text_token == html_token
    assert security_service.hash_token_string(text_token) == reg_token.token


def test_strip_name_and_email(
    client: TestClient,
):
    request_body = REGISTER_REQUEST_BODY.copy()
    request_body["name"] = " ivan  "
    request_body["email"] = " i@v.an  "

    with client:
        resp = client.post(
            REGISTRATION_PATH,
            json=request_body,
        )

    assert resp.json()["name"] == "ivan"
    assert resp.json()["email"] == "i@v.an"


@pytest.mark.parametrize(
    "request_body_updates,expected_error_loc,expected_error_key",
    (
        ({"name": {"a": "b"}}, ["body", "name"], "type_error.str"),
        ({"name": ""}, ["body", "name"], "value_error.any_str.min_length"),
        ({"name": "a"*51}, ["body", "name"], "value_error.any_str.max_length"),
        ({"email": ""}, ["body", "email"], "value_error.email"),
        (
            {"password": "simple"},
            ["body", "password"],
            "value_error.password.invalid"
        ),
    )
)
def test_registration_validation_errors(
    client: TestClient,
    request_body_updates: tp.Dict[str, tp.Any],
    expected_error_loc: tp.List[str],
    expected_error_key: str,
    fake_mailgun_server: FakeMailgunServer,
    db_session: orm.Session,
):
    request_body = REGISTER_REQUEST_BODY.copy()
    request_body.update(request_body_updates)
    with client:
        resp = client.post(
            REGISTRATION_PATH,
            json=request_body,
        )

    assert resp.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    resp_json = resp.json()
    assert resp_json["errors"][0]["error_loc"] == expected_error_loc
    assert resp_json["errors"][0]["error_key"] == expected_error_key

    assert_all_tables_are_empty(db_session)

    assert len(fake_mailgun_server.requests) == 0


def test_registration_when_user_exists(
    client: TestClient,
    create_db_object: tp.Callable[[Base], None],
    db_session: orm.Session,
    fake_mailgun_server: FakeMailgunServer,
) -> None:
    request_body = REGISTER_REQUEST_BODY.copy()
    email = request_body["email"]
    user = make_db_user(email=email)
    create_db_object(user)
    with client:
        resp = client.post(
            REGISTRATION_PATH,
            json=request_body,
        )
    assert resp.status_code == HTTPStatus.CONFLICT
    assert resp.json()["errors"][0]["error_key"] == "email.already_exists"

    assert_all_tables_are_empty(db_session, [UserTable])
    assert len(fake_mailgun_server.requests) == 0


def test_registration_when_newcomers_exist(
    client: TestClient,
    service_config: ServiceConfig,
    db_session: orm.Session,
    fake_mailgun_server: FakeMailgunServer,
) -> None:
    request_body = REGISTER_REQUEST_BODY.copy()
    max_same_newcomers = service_config.max_newcomers_with_same_email

    for i in range(max_same_newcomers + 1):
        with client:
            resp = client.post(
                REGISTRATION_PATH,
                json=request_body,
            )

        if i < max_same_newcomers:
            assert resp.status_code == HTTPStatus.CREATED
        else:
            assert resp.status_code == HTTPStatus.CONFLICT
            assert resp.json()["errors"][0]["error_key"] == "conflict"

    assert_all_tables_are_empty(
        db_session,
        [NewcomerTable, RegistrationTokenTable],
    )
    assert len(db_session.query(NewcomerTable).all()) == max_same_newcomers
    assert (
       len(db_session.query(RegistrationTokenTable).all())
       == max_same_newcomers
    )

    assert len(fake_mailgun_server.requests) == max_same_newcomers