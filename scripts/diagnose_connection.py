"""网络连接诊断脚本 - 排查 CLI 连接失败的原因"""

from __future__ import annotations

import asyncio
import os
import ssl
import sys
import socket
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

AGENT_CODE_ROOT = Path(__file__).resolve().parent.parent
if str(AGENT_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_CODE_ROOT))

from dotenv import load_dotenv


def print_section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f" {title}")
    print(f"{'='*60}")


def print_result(name: str, success: bool, details: str = "") -> None:
    status = "[PASS]" if success else "[FAIL]"
    print(f"  {status} {name}")
    if details:
        print(f"         {details}")


def diagnose_env_config() -> dict[str, str]:
    print_section("1. 环境变量配置检查")
    load_dotenv()

    results = {}

    api_key = os.getenv("OPENAI_API_KEY") or ""
    base_url = os.getenv("OPENAI_BASE_URL") or ""
    model = os.getenv("OPENAI_MODEL") or ""

    print_result(
        "OPENAI_API_KEY",
        bool(api_key),
        f"已配置 (长度: {len(api_key)} 字符, 前缀: {api_key[:8] if api_key else 'N/A'}...)"
    )
    results["api_key"] = api_key

    print_result(
        "OPENAI_BASE_URL",
        bool(base_url),
        base_url if base_url else "为空 - 将直连 OpenAI 官方 API"
    )
    results["base_url"] = base_url

    print_result(
        "OPENAI_MODEL",
        bool(model),
        model if model else "未配置 - 将使用默认模型"
    )
    results["model"] = model

    return results


def diagnose_ssl_context() -> bool:
    print_section("2. SSL 证书检查")

    try:
        default_context = ssl.create_default_context()
        print_result("创建默认 SSL Context", True, "成功")

        certs = default_context.get_ca_certs()
        print_result("CA 证书加载", True, f"共加载 {len(certs)} 个受信任 CA 证书")

        anaconda_ssl = os.getenv("SSL_CERT_DIR") or ""
        if anaconda_ssl:
            print_result("SSL_CERT_DIR 环境变量", True, anaconda_ssl)
        else:
            print_result("SSL_CERT_DIR 环境变量", True, "未设置 (使用系统默认)")

        return True
    except Exception as e:
        print_result("SSL Context 创建", False, str(e))
        return False


def diagnose_dns_and_network(target_host: str) -> bool:
    print_section(f"3. DNS 解析和网络连通性 - {target_host}")

    try:
        ip_address = socket.gethostbyname(target_host)
        print_result("DNS 解析", True, f"{target_host} -> {ip_address}")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10)
        if ":" in target_host:
            host, port = target_host.rsplit(":", 1)
            port = int(port)
        else:
            host = target_host
            port = 443

        result = sock.connect_ex((host, port))
        sock.close()

        print_result("TCP 连接到443端口", result == 0, f"{host}:{port}")

        if result != 0:
            print(f"         提示: 如果连接被拒绝，可能是防火墙或网络问题")

        return result == 0
    except socket.gaierror as e:
        print_result("DNS 解析", False, str(e))
        return False
    except Exception as e:
        print_result("网络连通性", False, str(e))
        return False


async def diagnose_openai_connection(api_key: str, base_url: str | None, model: str) -> bool:
    print_section("4. OpenAI API 实际连接测试")

    if not api_key:
        print_result("API Key 检查", False, "未配置 OPENAI_API_KEY")
        return False

    print_result("API Key 检查", True, f"Key 长度: {len(api_key)}")

    target_url = base_url if base_url else "https://api.openai.com"
    parsed = urlparse(target_url)
    host = parsed.netloc or parsed.path.split("/")[0]

    print(f"\n  尝试连接: {target_url}")

    try:
        from openai import AsyncOpenAI

        client_kwargs = {
            "api_key": api_key,
            "timeout": 30.0,
        }
        if base_url:
            client_kwargs["base_url"] = base_url

        print(f"  初始化 AsyncOpenAI 客户端...")
        client = AsyncOpenAI(**client_kwargs)

        print(f"  发送测试请求到 {host}...")

        try:
            response = await client.chat.completions.create(
                model=model or "gpt-3.5-turbo",
                messages=[{"role": "user", "content": "Hello"}],
                max_tokens=5,
            )

            print_result("API 请求", True, f"响应成功: {response.id}")
            return True

        except Exception as e:
            error_str = str(e)
            print_result("API 请求", False, error_str[:100])

            if "SSL" in error_str or "TLS" in error_str:
                print("\n  🔍 诊断: SSL/TLS 证书问题")
                print("     可能的解决方案:")
                print("     1. 更新系统的 CA 证书")
                print("     2. 设置 REQUESTS_CA_BUNDLE 环境变量指向证书文件")
                print("     3. 在 .env 中配置有效的 OPENAI_BASE_URL 代理")

            elif "Connection" in error_str or "连接" in error_str:
                print("\n  >> 网络连接问题")
                print("     可能的解决方案:")
                print("     1. 检查网络代理设置")
                print("     2. 配置 OPENAI_BASE_URL 为可用的代理地址")
                print("     3. 检查防火墙设置")

            elif "401" in error_str or "Unauthorized" in error_str:
                print("\n  🔍 诊断: API 认证失败")
                print("     可能的解决方案:")
                print("     1. 检查 OPENAI_API_KEY 是否正确")
                print("     2. 确认 API Key 是否还有额度")
                print("     3. 检查 Key 类型是否匹配 (如 sk-proj- 需要特定配置)")

            elif "404" in error_str or "Not Found" in error_str:
                print("\n  >> 端点不存在")
                print("     可能的解决方案:")
                print("     1. 检查 OPENAI_BASE_URL 是否正确")
                print("     2. 确认代理服务是否正常运行")

            elif "timeout" in error_str.lower():
                print("\n  🔍 诊断: 请求超时")
                print("     可能的解决方案:")
                print("     1. 检查网络延迟")
                print("     2. 配置代理加速访问")

            return False

    except Exception as e:
        print_result("客户端初始化", False, str(e))
        return False


def print_summary(success: bool, network_ok: bool, ssl_ok: bool, api_ok: bool) -> None:
    print_section("诊断总结")

    if success:
        print("  All tests passed! Connection should work properly.")
    else:
        print("  Problems found, possible causes and solutions:\n")

        if not network_ok:
            print("  [NETWORK] Network issue:")
            print("     - 检查您的网络是否能访问外网")
            print("     - 如果需要代理,，请在 .env 中配置 OPENAI_BASE_URL")
            print()

        if not ssl_ok:
            print("  [SSL] SSL Certificate issues:")
            print("     - Run: conda update ca-certificates")
            print("     - Or set env var: REQUESTS_CA_BUNDLE=/path/to/cert.pem")
            print()

        if not api_ok:
            print("  🤖 API 连接问题:")
            print("     - 检查 OPENAI_API_KEY 是否有效")
            print("     - 确认 OPENAI_BASE_URL 是否正确配置")
            print("     - 如果在中国大陆,可能需要使用代理服务")
            print()


async def main():
    print("=" * 60)
    print(" OpenAI Connection Diagnostic Tool")
    print(f" 诊断时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    config = diagnose_env_config()

    ssl_ok = diagnose_ssl_context()

    target_host = "api.openai.com"
    if config["base_url"]:
        parsed = urlparse(config["base_url"])
        target_host = parsed.netloc or parsed.path.split("/")[0]

    network_ok = diagnose_dns_and_network(target_host)

    api_ok = await diagnose_openai_connection(
        api_key=config["api_key"],
        base_url=config["base_url"] or None,
        model=config["model"] or "gpt-3.5-turbo"
    )

    print_summary(
        success=network_ok and ssl_ok and api_ok,
        network_ok=network_ok,
        ssl_ok=ssl_ok,
        api_ok=api_ok
    )

    return 0 if (network_ok and ssl_ok and api_ok) else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)