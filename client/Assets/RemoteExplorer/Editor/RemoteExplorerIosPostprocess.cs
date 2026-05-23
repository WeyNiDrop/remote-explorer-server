#if UNITY_EDITOR && UNITY_IOS
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using UnityEditor;
using UnityEditor.Callbacks;
using UnityEditor.iOS.Xcode;
using UnityEngine;

namespace RemoteExplorer.Editor
{
    public static class RemoteExplorerIosPostprocess
    {
        private const string EntitlementsFileName = "RemoteExplorer.entitlements";
        private const string MulticastNetworkingEntitlement = "com.apple.developer.networking.multicast";
        private const string UnityGraphicsInclude = "#include \"Unity/IUnityGraphics.h\"";
        private const string CompatibleUnityGraphicsInclude =
@"#if __has_include(""Unity/IUnityGraphics.h"")
#include ""Unity/IUnityGraphics.h""
#elif __has_include(""Tuanjie/IUnityGraphics.h"")
#include ""Tuanjie/IUnityGraphics.h""
#else
#include ""Unity/IUnityGraphics.h""
#endif";
        private const string AppStoreIconFileName = "RemoteExplorer-AppStore.png";
        private static readonly Encoding Utf8NoBom = new UTF8Encoding(false);
        private static readonly Regex MarketingIconEntryRegex = new Regex(
            @"\{(?=[^{}]*""idiom""\s*:\s*""ios-marketing"")[^{}]*\}",
            RegexOptions.Singleline);
        private static readonly Regex FilenameRegex = new Regex(@"""filename""\s*:\s*""[^""]*""");

        [PostProcessBuild(999)]
        public static void ConfigureIosExport(BuildTarget target, string pathToBuiltProject)
        {
            if (target != BuildTarget.iOS)
            {
                return;
            }

            PatchWebRtcUnityGraphicsInclude(pathToBuiltProject);
            AddLocalNetworkUsageDescription(pathToBuiltProject);
            ConfigureXcodeProject(pathToBuiltProject);
            EnsureAppStoreIcon(pathToBuiltProject);
        }

        private static void AddLocalNetworkUsageDescription(string pathToBuiltProject)
        {
            var plistPath = Path.Combine(pathToBuiltProject, "Info.plist");
            var plist = new PlistDocument();
            plist.ReadFromFile(plistPath);
            plist.root.SetString(
                "NSLocalNetworkUsageDescription",
                "Remote Explorer uses the local network to discover and control your browser server.");
            plist.WriteToFile(plistPath);
        }

        private static void ConfigureXcodeProject(string pathToBuiltProject)
        {
            var projectPath = PBXProject.GetPBXProjectPath(pathToBuiltProject);
            var project = new PBXProject();
            project.ReadFromFile(projectPath);

            var mainTargetGuid = project.GetUnityMainTargetGuid();
            project.SetBuildProperty(mainTargetGuid, "ENABLE_USER_SCRIPT_SANDBOXING", "NO");

            var frameworkTargetGuid = project.GetUnityFrameworkTargetGuid();
            if (!string.IsNullOrEmpty(frameworkTargetGuid))
            {
                project.SetBuildProperty(frameworkTargetGuid, "ENABLE_USER_SCRIPT_SANDBOXING", "NO");
                project.SetBuildProperty(frameworkTargetGuid, "ENABLE_MODULE_VERIFIER", "NO");
            }

            var entitlementsProjectPath = project.GetBuildPropertyForAnyConfig(
                mainTargetGuid,
                "CODE_SIGN_ENTITLEMENTS");
            if (string.IsNullOrEmpty(entitlementsProjectPath))
            {
                entitlementsProjectPath = EntitlementsFileName;
                project.AddFile(entitlementsProjectPath, entitlementsProjectPath);
                project.SetBuildProperty(
                    mainTargetGuid,
                    "CODE_SIGN_ENTITLEMENTS",
                    entitlementsProjectPath);
            }

            var entitlementsPath = Path.Combine(pathToBuiltProject, entitlementsProjectPath);
            var entitlementsDirectory = Path.GetDirectoryName(entitlementsPath);
            if (!string.IsNullOrEmpty(entitlementsDirectory))
            {
                Directory.CreateDirectory(entitlementsDirectory);
            }

            var entitlements = new PlistDocument();
            if (File.Exists(entitlementsPath))
            {
                entitlements.ReadFromFile(entitlementsPath);
            }
            else
            {
                entitlements.Create();
            }

            entitlements.root.SetBoolean(MulticastNetworkingEntitlement, true);
            entitlements.WriteToFile(entitlementsPath);
            project.WriteToFile(projectPath);

            PatchXcodeBuildSettingsText(projectPath);
        }

        private static void PatchWebRtcUnityGraphicsInclude(string pathToBuiltProject)
        {
            var registerPluginPath = Path.Combine(
                pathToBuiltProject,
                "Libraries",
                "com.unity.webrtc",
                "Runtime",
                "Plugins",
                "iOS",
                "RegisterPlugin.mm");

            if (!File.Exists(registerPluginPath))
            {
                return;
            }

            var contents = File.ReadAllText(registerPluginPath);
            if (contents.Contains("Tuanjie/IUnityGraphics.h") || !contents.Contains(UnityGraphicsInclude))
            {
                return;
            }

            File.WriteAllText(
                registerPluginPath,
                contents.Replace(UnityGraphicsInclude, CompatibleUnityGraphicsInclude),
                Utf8NoBom);
        }

        private static void PatchXcodeBuildSettingsText(string projectPath)
        {
            if (!File.Exists(projectPath))
            {
                return;
            }

            var contents = File.ReadAllText(projectPath);
            var updatedContents = contents
                .Replace("ENABLE_USER_SCRIPT_SANDBOXING = YES;", "ENABLE_USER_SCRIPT_SANDBOXING = NO;")
                .Replace("ENABLE_MODULE_VERIFIER = YES;", "ENABLE_MODULE_VERIFIER = NO;");

            if (updatedContents != contents)
            {
                File.WriteAllText(projectPath, updatedContents, Utf8NoBom);
            }
        }

        private static void EnsureAppStoreIcon(string pathToBuiltProject)
        {
            var appIconSets = Directory.GetDirectories(
                pathToBuiltProject,
                "AppIcon.appiconset",
                SearchOption.AllDirectories);
            if (appIconSets.Length == 0)
            {
                return;
            }

            var appIconSetPath = appIconSets[0];
            var iconPath = Path.Combine(appIconSetPath, AppStoreIconFileName);
            File.WriteAllBytes(iconPath, CreateAppStoreIconPng());

            var contentsJsonPath = Path.Combine(appIconSetPath, "Contents.json");
            if (!File.Exists(contentsJsonPath))
            {
                WriteMinimalAppIconContents(contentsJsonPath);
                return;
            }

            var contentsJson = File.ReadAllText(contentsJsonPath);
            if (contentsJson.Contains("\"ios-marketing\"") && contentsJson.Contains(AppStoreIconFileName))
            {
                return;
            }

            if (contentsJson.Contains("\"ios-marketing\""))
            {
                File.WriteAllText(contentsJsonPath, UpsertMarketingIconFileName(contentsJson), Utf8NoBom);
                return;
            }

            var marker = "\n  ],";
            var marketingIconEntry =
                "    {\n" +
                "      \"filename\" : \"" + AppStoreIconFileName + "\",\n" +
                "      \"idiom\" : \"ios-marketing\",\n" +
                "      \"scale\" : \"1x\",\n" +
                "      \"size\" : \"1024x1024\"\n" +
                "    }";

            if (contentsJson.Contains(marker))
            {
                var needsComma = contentsJson.Contains("\"images\"") && !contentsJson.Contains("\"images\" : [\n  ]");
                var insertion = (needsComma ? ",\n" : "\n") + marketingIconEntry;
                File.WriteAllText(contentsJsonPath, contentsJson.Replace(marker, insertion + marker), Utf8NoBom);
            }
            else
            {
                WriteMinimalAppIconContents(contentsJsonPath);
            }
        }

        private static string UpsertMarketingIconFileName(string contentsJson)
        {
            var match = MarketingIconEntryRegex.Match(contentsJson);
            if (!match.Success)
            {
                return contentsJson;
            }

            var entry = match.Value;
            if (FilenameRegex.IsMatch(entry))
            {
                entry = FilenameRegex.Replace(
                    entry,
                    "\"filename\" : \"" + AppStoreIconFileName + "\"",
                    1);
            }
            else
            {
                entry = entry.Replace(
                    "\"idiom\"",
                    "\"filename\" : \"" + AppStoreIconFileName + "\",\n      \"idiom\"");
            }

            return contentsJson.Substring(0, match.Index) +
                entry +
                contentsJson.Substring(match.Index + match.Length);
        }

        private static void WriteMinimalAppIconContents(string contentsJsonPath)
        {
            var contentsJson =
                "{\n" +
                "  \"images\" : [\n" +
                "    {\n" +
                "      \"filename\" : \"" + AppStoreIconFileName + "\",\n" +
                "      \"idiom\" : \"ios-marketing\",\n" +
                "      \"scale\" : \"1x\",\n" +
                "      \"size\" : \"1024x1024\"\n" +
                "    }\n" +
                "  ],\n" +
                "  \"info\" : {\n" +
                "    \"author\" : \"xcode\",\n" +
                "    \"version\" : 1\n" +
                "  }\n" +
                "}\n";
            File.WriteAllText(contentsJsonPath, contentsJson, Utf8NoBom);
        }

        private static byte[] CreateAppStoreIconPng()
        {
            const int size = 1024;
            var texture = new Texture2D(size, size, TextureFormat.RGBA32, false);
            var pixels = new Color32[size * size];

            for (var y = 0; y < size; y++)
            {
                for (var x = 0; x < size; x++)
                {
                    pixels[y * size + x] = IconPixel(size, x, y);
                }
            }

            texture.SetPixels32(pixels);
            texture.Apply(false, false);
            var png = texture.EncodeToPNG();
            Object.DestroyImmediate(texture);
            return png;
        }

        private static Color32 IconPixel(int size, int x, int y)
        {
            var nx = (x + 0.5f) / size;
            var ny = (y + 0.5f) / size;
            var t = nx * 0.45f + ny * 0.55f;
            var baseColor = new Color32(
                (byte)(26 + 20 * t),
                (byte)(92 + 70 * t),
                (byte)(168 + 45 * t),
                255);

            if (InsideRoundedRect(nx, ny, 0.20f, 0.24f, 0.80f, 0.66f, 0.055f))
            {
                baseColor = Blend(baseColor, new Color32(238, 248, 255, 238));
            }

            if (InsideRoundedRect(nx, ny, 0.245f, 0.295f, 0.755f, 0.595f, 0.028f))
            {
                baseColor = Blend(baseColor, new Color32(28, 45, 78, 255));
            }

            if (0.38f <= nx && nx <= 0.62f && 0.66f <= ny && ny <= 0.72f)
            {
                baseColor = Blend(baseColor, new Color32(238, 248, 255, 236));
            }

            if (0.31f <= nx && nx <= 0.69f && Mathf.Abs(ny - 0.75f) <= 0.022f)
            {
                baseColor = Blend(baseColor, new Color32(238, 248, 255, 236));
            }

            if ((nx - 0.63f) * (nx - 0.63f) + (ny - 0.43f) * (ny - 0.43f) <= 0.055f * 0.055f)
            {
                baseColor = Blend(baseColor, new Color32(79, 214, 145, 255));
            }

            if (0.33f <= nx && nx <= 0.55f && Mathf.Abs((ny - 0.46f) - 0.18f * (nx - 0.33f)) <= 0.022f)
            {
                baseColor = Blend(baseColor, new Color32(79, 214, 145, 230));
            }

            return baseColor;
        }

        private static bool InsideRoundedRect(
            float x,
            float y,
            float left,
            float top,
            float right,
            float bottom,
            float radius)
        {
            if (x < left || x > right || y < top || y > bottom)
            {
                return false;
            }

            var cx = Mathf.Min(Mathf.Max(x, left + radius), right - radius);
            var cy = Mathf.Min(Mathf.Max(y, top + radius), bottom - radius);
            return (x - cx) * (x - cx) + (y - cy) * (y - cy) <= radius * radius;
        }

        private static Color32 Blend(Color32 destination, Color32 source)
        {
            var alpha = source.a / 255f;
            var inverseAlpha = 1f - alpha;
            return new Color32(
                (byte)(source.r * alpha + destination.r * inverseAlpha),
                (byte)(source.g * alpha + destination.g * inverseAlpha),
                (byte)(source.b * alpha + destination.b * inverseAlpha),
                255);
        }
    }
}
#endif
