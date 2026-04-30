using System;
using System.IO;
using Newtonsoft.Json;
using UnityEngine;

namespace MtgInventoryPayload
{
    public static class Loader
    {
        public const string GameObjectName = "MtgToolkitInventoryDumper";

        // SharpMonoInjector's mono_class_get_method_from_name call passes
        // param_count=0 and mono_runtime_invoke gets NULL params, so the
        // entry point MUST be parameterless. The injector communicates
        // the output path via a JSON sidecar file at a known location
        // inside the Wine TEMP dir, which both processes share.
        public const string ConfigDirName = "mtg-toolkit-inject";
        public const string ConfigFileName = "config.json";

        public static int Load()
        {
            string outPath = null;
            try
            {
                outPath = ResolveOutPath();

                var existing = GameObject.Find(GameObjectName);
                if (existing != null) UnityEngine.Object.Destroy(existing);

                var go = new GameObject(GameObjectName);
                var dumper = go.AddComponent<InventoryDumper>();
                dumper.OutPath = outPath;
                UnityEngine.Object.DontDestroyOnLoad(go);
                return 0;
            }
            catch (Exception ex)
            {
                try
                {
                    var errPath = (outPath ?? Path.Combine(Path.GetTempPath(), "mtg-toolkit-collection.json")) + ".err";
                    File.WriteAllText(errPath, ex.ToString());
                }
                catch { }
                return 1;
            }
        }

        private static string ResolveOutPath()
        {
            var configPath = Path.Combine(Path.GetTempPath(), ConfigDirName, ConfigFileName);
            var fallback = Path.Combine(Path.GetTempPath(), "mtg-toolkit-collection.json");
            string reason = null;
            if (!File.Exists(configPath))
            {
                reason = "sidecar config not found at " + configPath;
            }
            else
            {
                try
                {
                    var json = File.ReadAllText(configPath);
                    var cfg = JsonConvert.DeserializeObject<InjectConfig>(json);
                    if (cfg != null && !string.IsNullOrEmpty(cfg.out_path))
                        return cfg.out_path;
                    reason = "sidecar config at " + configPath + " parsed but had no out_path";
                }
                catch (Exception ex)
                {
                    reason = "failed to parse sidecar config at " + configPath + ": " + ex.Message;
                }
            }
            try
            {
                File.WriteAllText(
                    fallback + ".err",
                    "ResolveOutPath fallback engaged: " + reason
                    + Environment.NewLine
                    + "Using hardcoded fallback path: " + fallback);
            }
            catch { }
            return fallback;
        }

        private class InjectConfig
        {
            public string out_path { get; set; }
        }
    }

    public class InventoryDumper : MonoBehaviour
    {
        public string OutPath;

        private const float PollInterval = 1.0f;
        private const float TimeoutSeconds = 120f;

        private float _lastPoll;
        private float _deadline;

        void Start()
        {
            _deadline = Time.realtimeSinceStartup + TimeoutSeconds;
            _lastPoll = 0f;
        }

        void Update()
        {
            if (Time.realtimeSinceStartup - _lastPoll < PollInterval) return;
            _lastPoll = Time.realtimeSinceStartup;

            try
            {
                var wc = WrapperController.Instance;
                if (wc != null && wc.InventoryManager != null
                    && wc.InventoryManager.Cards != null
                    && wc.InventoryManager.Cards.Count > 0)
                {
                    DumpAndDestroy(wc.InventoryManager.Cards);
                    return;
                }
                if (Time.realtimeSinceStartup > _deadline)
                {
                    File.WriteAllText(
                        OutPath + ".err",
                        "timeout: WrapperController.Instance.InventoryManager.Cards "
                        + "did not populate within " + TimeoutSeconds + "s. "
                        + "Sign in to MTGA's main menu and try again.");
                    UnityEngine.Object.Destroy(gameObject);
                }
            }
            catch (Exception ex)
            {
                try { File.WriteAllText(OutPath + ".err", ex.ToString()); } catch { }
                UnityEngine.Object.Destroy(gameObject);
            }
        }

        private void DumpAndDestroy(object cards)
        {
            try
            {
                var json = JsonConvert.SerializeObject(cards);
                // Write to a sibling .tmp first, then atomically rename so
                // the Python parent never observes a half-written file: it
                // sees either the previous OutPath (or no file) or the
                // complete new dump, never a torn read.
                var tmpPath = OutPath + ".tmp";
                if (File.Exists(tmpPath)) File.Delete(tmpPath);
                File.WriteAllText(tmpPath, json);
                if (File.Exists(OutPath)) File.Delete(OutPath);
                File.Move(tmpPath, OutPath);
            }
            catch (Exception ex)
            {
                try { File.WriteAllText(OutPath + ".err", ex.ToString()); } catch { }
            }
            finally
            {
                UnityEngine.Object.Destroy(gameObject);
            }
        }
    }
}
