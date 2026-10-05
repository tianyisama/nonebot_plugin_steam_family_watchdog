"""Steam Authentication protobuf 消息的最小描述符集合。

字段号/类型对应 steammessages_auth.steamclient.proto，使用 protobuf 库
处理 wire format 和未知字段，无需 Node.js、protoc 或手写二进制编解码。
协议来源见 README；只定义本项目用到的认证方法。
"""

from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

F = descriptor_pb2.FieldDescriptorProto
SCHEMAS = {
    "DeviceDetails": [
        (1, "device_friendly_name", F.TYPE_STRING), (2, "platform_type", F.TYPE_INT32),
        (3, "os_type", F.TYPE_INT32), (4, "gaming_device_type", F.TYPE_UINT32),
    ],
    "AllowedConfirmation": [
        (1, "confirmation_type", F.TYPE_INT32), (2, "associated_message", F.TYPE_STRING),
    ],
    "GetPasswordRSAPublicKey_Request": [(1, "account_name", F.TYPE_STRING)],
    "GetPasswordRSAPublicKey_Response": [
        (1, "publickey_mod", F.TYPE_STRING), (2, "publickey_exp", F.TYPE_STRING), (3, "timestamp", F.TYPE_UINT64),
    ],
    "BeginAuthSessionViaCredentials_Request": [
        (2, "account_name", F.TYPE_STRING), (3, "encrypted_password", F.TYPE_STRING),
        (4, "encryption_timestamp", F.TYPE_UINT64), (5, "remember_login", F.TYPE_BOOL),
        (7, "persistence", F.TYPE_INT32), (8, "website_id", F.TYPE_STRING),
        (9, "device_details", "DeviceDetails"),
    ],
    "BeginAuthSessionViaCredentials_Response": [
        (1, "client_id", F.TYPE_UINT64), (2, "request_id", F.TYPE_BYTES), (3, "interval", F.TYPE_FLOAT),
        (4, "allowed_confirmations", "AllowedConfirmation", True), (5, "steamid", F.TYPE_UINT64),
    ],
    "BeginAuthSessionViaQR_Request": [(3, "device_details", "DeviceDetails")],
    "BeginAuthSessionViaQR_Response": [
        (1, "client_id", F.TYPE_UINT64), (2, "challenge_url", F.TYPE_STRING),
        (3, "request_id", F.TYPE_BYTES), (4, "interval", F.TYPE_FLOAT),
        (5, "allowed_confirmations", "AllowedConfirmation", True), (6, "version", F.TYPE_INT32),
    ],
    "PollAuthSessionStatus_Request": [(1, "client_id", F.TYPE_UINT64), (2, "request_id", F.TYPE_BYTES)],
    "PollAuthSessionStatus_Response": [
        (1, "new_client_id", F.TYPE_UINT64), (2, "new_challenge_url", F.TYPE_STRING),
        (3, "refresh_token", F.TYPE_STRING), (4, "access_token", F.TYPE_STRING),
        (5, "had_remote_interaction", F.TYPE_BOOL), (6, "account_name", F.TYPE_STRING),
    ],
    "UpdateAuthSessionWithSteamGuardCode_Request": [
        (1, "client_id", F.TYPE_UINT64), (2, "steamid", F.TYPE_FIXED64),
        (3, "code", F.TYPE_STRING), (4, "code_type", F.TYPE_INT32),
    ],
    "UpdateAuthSessionWithSteamGuardCode_Response": [],
    "GenerateAccessTokenForApp_Request": [
        (1, "refresh_token", F.TYPE_STRING), (2, "steamid", F.TYPE_FIXED64), (3, "renewal_type", F.TYPE_INT32),
    ],
    "GenerateAccessTokenForApp_Response": [(1, "access_token", F.TYPE_STRING), (2, "refresh_token", F.TYPE_STRING)],
}


def _build_messages():
    file = descriptor_pb2.FileDescriptorProto(name="watchdog_auth.proto", package="watchdog.auth", syntax="proto2")
    for name, fields in SCHEMAS.items():
        message = file.message_type.add(name=name)
        for number, field_name, field_type, *repeated in fields:
            field = message.field.add(name=field_name, number=number,
                label=F.LABEL_REPEATED if repeated and repeated[0] else F.LABEL_OPTIONAL)
            if isinstance(field_type, str):
                field.type = F.TYPE_MESSAGE
                field.type_name = f".watchdog.auth.{field_type}"
            else:
                field.type = field_type
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    return {name: message_factory.GetMessageClass(pool.FindMessageTypeByName(f"watchdog.auth.{name}")) for name in SCHEMAS}


MESSAGES = _build_messages()


def message(name: str, **values):
    return MESSAGES[name](**values)
