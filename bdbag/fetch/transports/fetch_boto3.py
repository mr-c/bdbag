import os
import datetime
import logging
from importlib import import_module
from bdbag import urlsplit, urlunsplit, stob, get_typed_exception
from bdbag.bdbag_config import DEFAULT_CONFIG, DEFAULT_FETCH_CONFIG, FETCH_CONFIG_TAG
from bdbag.fetch import Megabyte, get_transfer_summary
from bdbag.fetch.auth import keychain

logger = logging.getLogger(__name__)

BOTO3 = None
BOTOCORE = None
CHUNK_SIZE = 10 * Megabyte


def import_boto3():
    # locate library
    global BOTO3, BOTOCORE
    if BOTO3 is None and BOTOCORE is None:
        try:
            BOTO3 = import_module("boto3")
            BOTOCORE = import_module("botocore")
        except ImportError as e:
            raise RuntimeError(
                "Unable to find required module. Ensure that the Python package \"boto3\" is installed.", e)
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        BOTO3.set_stream_logger('')


def validate_auth_config(auth):
    if not keychain.has_auth_attr(auth, 'auth_type'):
        return False

    return True


def get_credentials(url, auth_config):

    credentials = None
    for auth in list((entry for entry in auth_config if hasattr(entry, 'uri') and (entry.uri.lower() in url.lower()))):

        if not validate_auth_config(auth):
            continue

        if auth.auth_type == 'aws-credentials':
            if keychain.has_auth_attr(auth, "auth_params"):
                credentials = auth.auth_params
                break

    return credentials


def get_file(url, output_path, auth_config, **kwargs):
    success = False
    try:
        import_boto3()

        bdbag_config = kwargs.get("config", DEFAULT_CONFIG)
        fetch_config = bdbag_config.get(FETCH_CONFIG_TAG, DEFAULT_FETCH_CONFIG)
        config = fetch_config.get("s3", DEFAULT_FETCH_CONFIG["s3"])
        credentials = get_credentials(url, auth_config)
        key = credentials.key if keychain.has_auth_attr(credentials, "key", quiet=True) else None
        secret = credentials.secret if keychain.has_auth_attr(credentials, "secret", quiet=True) else None
        token = credentials.token if keychain.has_auth_attr(credentials, "token", quiet=True) else None
        role_arn = credentials.role_arn if keychain.has_auth_attr(credentials, "role_arn", quiet=True) else None
        profile_name = credentials.profile if keychain.has_auth_attr(credentials, "profile", quiet=True) else None

        try:
            session = BOTO3.session.Session(profile_name=profile_name)
        except Exception as e:
            raise RuntimeError("Unable to create Boto3 session: %s" % get_typed_exception(e))

        if role_arn:
            try:
                sts = session.client('sts')
                response = sts.assume_role(RoleArn=role_arn, RoleSessionName='BDBag-Fetch', DurationSeconds=3600)
                temp_credentials = response['Credentials']
                key = temp_credentials['AccessKeyId']
                secret = temp_credentials['SecretAccessKey']
                token = temp_credentials['SessionToken']
            except Exception as e:
                raise RuntimeError(
                    "Unable to get temporary credentials using arn [%s]. %s" % (role_arn, get_typed_exception(e)))

        upr = urlsplit(url, allow_fragments=False)
        try:
            if upr.scheme == "gs":
                endpoint_url = "https://storage.googleapis.com"
                config = BOTO3.session.Config(signature_version="s3v4")
                kwargs = {"aws_access_key_id": key,
                          "aws_secret_access_key": secret,
                          "endpoint_url": endpoint_url,
                          "config": config}
            else:
                kwargs = {"aws_access_key_id": key, "aws_secret_access_key": secret}
                if token:
                    kwargs.update({"aws_session_token": token})
            s3_client = session.client("s3", **kwargs)
        except Exception as e:
            raise RuntimeError("Unable to create Boto3 storage client: %s" % get_typed_exception(e))

        output_dir = os.path.dirname(os.path.abspath(output_path))
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        logger.info("Attempting GET from URL: %s" % url)
        response = s3_client.get_object(Bucket=upr.netloc, Key=upr.path.lstrip("/"))
        chunk_size = config.get("read_chunk_size", CHUNK_SIZE)
        max_retries = config.get("max_read_retries", 5)
        retry_count = 0
        total = 0

        logger.debug("Transferring file %s to %s" % (url, output_path))
        start = datetime.datetime.now()
        with open(output_path, 'wb') as data_file:
            stream = response["Body"]
            stream.set_socket_timeout(config.get("read_timeout_seconds", 120))
            chunk = None
            while True:
                while retry_count < max_retries:
                    try:
                        chunk = stream.read(chunk_size)
                        break
                    except BOTOCORE.exceptions.ReadTimeoutError as rt:
                        retry_count += 1
                        logging.warning("Boto3 read timeout. Retrying attempt %s of %s" % (retry_count, max_retries))
                        if retry_count == max_retries:
                            raise rt
                if chunk == b"" or chunk is None:
                    break
                data_file.write(chunk)
                total += len(chunk)
            stream.close()
        elapsed_time = datetime.datetime.now() - start
        summary = get_transfer_summary(total, elapsed_time)
        logger.info('File [%s] transfer successful. %s' % (output_path, summary))
        success = True
    except BOTOCORE.exceptions.ClientError as e:
        logger.error('Boto3 Client Error: %s' % get_typed_exception(e))
    except BOTOCORE.exceptions.BotoCoreError as e:
        logger.error('Boto3 Error: %s' % get_typed_exception(e))
    except Exception as e:
        logger.error(get_typed_exception(e))
    finally:
        if not success:
            logger.error('Boto3 GET Failed for URL: %s' % url)
            logger.warning('File transfer failed: [%s]' % output_path)

    return success
