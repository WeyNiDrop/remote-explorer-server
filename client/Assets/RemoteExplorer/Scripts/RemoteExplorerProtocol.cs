using System;
using System.Collections;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using UnityEngine;

namespace RemoteExplorer
{
    public static class RemoteExplorerProtocol
    {
        public const int Version = 1;
        public const int DefaultDiscoveryPort = 45454;
        public const int DefaultControlPort = 45455;
        public const int Pbkdf2Iterations = 100000;

        public static string NewRequestId()
        {
            return Guid.NewGuid().ToString("N");
        }

        public static byte[] Encode(Dictionary<string, object> message)
        {
            return Encoding.UTF8.GetBytes(CanonicalJson.Serialize(message));
        }

        public static byte[] DerivePasswordKey(string password, string saltHex, int iterations)
        {
            var salt = HexToBytes(saltHex);
            return Pbkdf2Sha256(Encoding.UTF8.GetBytes(password), salt, iterations, 32);
        }

        public static string HmacHex(byte[] key, string message)
        {
            using (var hmac = new HMACSHA256(key))
            {
                return BytesToHex(hmac.ComputeHash(Encoding.UTF8.GetBytes(message)));
            }
        }

        public static byte[] HmacBytes(byte[] key, string message)
        {
            using (var hmac = new HMACSHA256(key))
            {
                return hmac.ComputeHash(Encoding.UTF8.GetBytes(message));
            }
        }

        public static string ProofMessage(string clientId, string serverNonce, string clientNonce)
        {
            return $"proof:{clientId}:{serverNonce}:{clientNonce}";
        }

        public static string SessionMessage(string clientId, string serverNonce, string clientNonce)
        {
            return $"session:{clientId}:{serverNonce}:{clientNonce}";
        }

        public static string SignMessage(Dictionary<string, object> message, byte[] sessionKey)
        {
            return HmacHex(sessionKey, CanonicalJson.Serialize(message));
        }

        public static string BytesToHex(byte[] bytes)
        {
            var builder = new StringBuilder(bytes.Length * 2);
            foreach (var value in bytes)
            {
                builder.Append(value.ToString("x2", CultureInfo.InvariantCulture));
            }
            return builder.ToString();
        }

        public static byte[] HexToBytes(string hex)
        {
            if (hex.Length % 2 != 0)
            {
                throw new ArgumentException("Hex string must have an even length.", nameof(hex));
            }

            var result = new byte[hex.Length / 2];
            for (var i = 0; i < result.Length; i++)
            {
                result[i] = Convert.ToByte(hex.Substring(i * 2, 2), 16);
            }

            return result;
        }

        private static byte[] Pbkdf2Sha256(byte[] password, byte[] salt, int iterations, int byteCount)
        {
            var hashLength = 32;
            var blockCount = (int)Math.Ceiling((double)byteCount / hashLength);
            var output = new byte[blockCount * hashLength];

            using (var hmac = new HMACSHA256(password))
            {
                for (var block = 1; block <= blockCount; block++)
                {
                    var blockBytes = BitConverter.GetBytes(IPAddress.HostToNetworkOrder(block));
                    var input = new byte[salt.Length + 4];
                    Buffer.BlockCopy(salt, 0, input, 0, salt.Length);
                    Buffer.BlockCopy(blockBytes, 0, input, salt.Length, 4);

                    var u = hmac.ComputeHash(input);
                    var t = new byte[hashLength];
                    Buffer.BlockCopy(u, 0, t, 0, hashLength);

                    for (var i = 1; i < iterations; i++)
                    {
                        u = hmac.ComputeHash(u);
                        for (var j = 0; j < hashLength; j++)
                        {
                            t[j] ^= u[j];
                        }
                    }

                    Buffer.BlockCopy(t, 0, output, (block - 1) * hashLength, hashLength);
                }
            }

            var result = new byte[byteCount];
            Buffer.BlockCopy(output, 0, result, 0, byteCount);
            return result;
        }

        public static string ClientId()
        {
            if (!string.IsNullOrEmpty(SystemInfo.deviceUniqueIdentifier))
            {
                return SystemInfo.deviceUniqueIdentifier;
            }

            return SystemInfo.deviceName + "-" + Application.platform;
        }

        public static string ClientName()
        {
            if (!string.IsNullOrWhiteSpace(SystemInfo.deviceName))
            {
                return SystemInfo.deviceName;
            }

            return "Unity Client";
        }
    }

    public static class CanonicalJson
    {
        public static string Serialize(object value)
        {
            switch (value)
            {
                case null:
                    return "null";
                case string text:
                    return Quote(text);
                case bool flag:
                    return flag ? "true" : "false";
                case int number:
                    return number.ToString(CultureInfo.InvariantCulture);
                case long number:
                    return number.ToString(CultureInfo.InvariantCulture);
                case float number:
                    return number.ToString("R", CultureInfo.InvariantCulture);
                case double number:
                    return number.ToString("R", CultureInfo.InvariantCulture);
                case decimal number:
                    return number.ToString(CultureInfo.InvariantCulture);
                case IDictionary<string, object> dict:
                    return SerializeDictionary(dict);
                default:
                    if (!(value is string) && value is IEnumerable enumerable)
                    {
                        return SerializeArray(enumerable);
                    }

                    return Quote(Convert.ToString(value, CultureInfo.InvariantCulture) ?? string.Empty);
            }
        }

        private static string SerializeDictionary(IDictionary<string, object> dict)
        {
            var parts = dict
                .OrderBy(pair => pair.Key, StringComparer.Ordinal)
                .Select(pair => Quote(pair.Key) + ":" + Serialize(pair.Value));
            return "{" + string.Join(",", parts) + "}";
        }

        private static string SerializeArray(IEnumerable enumerable)
        {
            var parts = new List<string>();
            foreach (var value in enumerable)
            {
                parts.Add(Serialize(value));
            }

            return "[" + string.Join(",", parts) + "]";
        }

        private static string Quote(string value)
        {
            var builder = new StringBuilder(value.Length + 2);
            builder.Append('"');
            foreach (var ch in value)
            {
                switch (ch)
                {
                    case '"':
                        builder.Append("\\\"");
                        break;
                    case '\\':
                        builder.Append("\\\\");
                        break;
                    case '\b':
                        builder.Append("\\b");
                        break;
                    case '\f':
                        builder.Append("\\f");
                        break;
                    case '\n':
                        builder.Append("\\n");
                        break;
                    case '\r':
                        builder.Append("\\r");
                        break;
                    case '\t':
                        builder.Append("\\t");
                        break;
                    default:
                        if (ch < 32)
                        {
                            builder.Append("\\u");
                            builder.Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            builder.Append(ch);
                        }

                        break;
                }
            }

            builder.Append('"');
            return builder.ToString();
        }
    }

    [Serializable]
    public class ServerOfferEnvelope
    {
        public int v;
        public string type;
        public string request_id;
        public ServerOffer server;
    }

    [Serializable]
    public class ServerOffer
    {
        public string id;
        public string name;
        public int control_port;
        public string auth;
        public string[] capabilities;
    }

    [Serializable]
    public class AuthEnvelope
    {
        public int v;
        public string type;
        public string request_id;
        public AuthChallenge challenge;
        public AuthSession session;
        public ErrorBody error;
    }

    [Serializable]
    public class AuthChallenge
    {
        public string server_nonce;
        public string salt;
        public int iterations;
        public string algorithm;
    }

    [Serializable]
    public class AuthSession
    {
        public string id;
        public string auth;
    }

    [Serializable]
    public class CommandEnvelope
    {
        public int v;
        public string type;
        public string request_id;
        public bool ok;
        public WebRtcAnswer result;
        public ErrorBody error;
    }

    [Serializable]
    public class WebRtcAnswer
    {
        public string transport;
        public string codec;
        public string peer_id;
        public string type;
        public string sdp;
        public string resolution;
        public int width;
        public int height;
        public int fps;
        public bool available;
        public string error;
    }

    [Serializable]
    public class ErrorBody
    {
        public string code;
        public string message;
    }

    public class DiscoveredServer
    {
        public string Address;
        public int ControlPort;
        public string Id;
        public string Name;
        public string AuthMode;
        public string[] Capabilities;

        public bool RequiresPassword => string.Equals(AuthMode, "password", StringComparison.OrdinalIgnoreCase);

        public override string ToString()
        {
            var displayName = string.IsNullOrEmpty(Name) ? "Remote Explorer" : Name;
            var auth = RequiresPassword ? "password" : "open";
            return $"{displayName} ({Address}:{ControlPort}, {auth})";
        }
    }

    public class RemoteExplorerSettings
    {
        private const string KeyPassword = "RemoteExplorer.Password";
        private const string KeyAutoConnect = "RemoteExplorer.AutoConnect";
        private const string KeyServerId = "RemoteExplorer.ServerId";
        private const string KeyServerHost = "RemoteExplorer.ServerHost";
        private const string KeyServerPort = "RemoteExplorer.ServerPort";

        public string Password;
        public bool AutoConnect;
        public string ServerId;
        public string ServerHost;
        public int ServerPort = RemoteExplorerProtocol.DefaultControlPort;

        public static RemoteExplorerSettings Load()
        {
            return new RemoteExplorerSettings
            {
                Password = PlayerPrefs.GetString(KeyPassword, string.Empty),
                AutoConnect = PlayerPrefs.GetInt(KeyAutoConnect, 0) == 1,
                ServerId = PlayerPrefs.GetString(KeyServerId, string.Empty),
                ServerHost = PlayerPrefs.GetString(KeyServerHost, string.Empty),
                ServerPort = PlayerPrefs.GetInt(KeyServerPort, RemoteExplorerProtocol.DefaultControlPort)
            };
        }

        public void Save()
        {
            PlayerPrefs.SetString(KeyPassword, Password ?? string.Empty);
            PlayerPrefs.SetInt(KeyAutoConnect, AutoConnect ? 1 : 0);
            PlayerPrefs.SetString(KeyServerId, ServerId ?? string.Empty);
            PlayerPrefs.SetString(KeyServerHost, ServerHost ?? string.Empty);
            PlayerPrefs.SetInt(KeyServerPort, ServerPort);
            PlayerPrefs.Save();
        }
    }
}
