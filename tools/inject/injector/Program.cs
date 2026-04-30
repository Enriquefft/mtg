using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using Newtonsoft.Json;
using SharpMonoInjector;

namespace MtgInject
{
    /// <summary>
    /// Console exe (net48) that injects MtgInventoryPayload.dll into a
    /// running MTGA.exe process and invokes its parameterless entry
    /// point. The output path is communicated via a JSON sidecar at
    /// %TEMP%/mtg-toolkit-inject/config.json — both the injector and
    /// the injected payload share the same Wine prefix, so Path.GetTempPath()
    /// resolves identically in both processes.
    /// </summary>
    internal static class Program
    {
        private const string TargetProcess = "MTGA";
        private const string PayloadNamespace = "MtgInventoryPayload";
        private const string PayloadClass = "Loader";
        private const string PayloadMethod = "Load";
        private const string ConfigDir = "mtg-toolkit-inject";
        private const string ConfigFile = "config.json";

        private static int Main(string[] args)
        {
            string payloadPath = null;
            string outPath = null;

            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i])
                {
                    case "--payload":
                        payloadPath = args[++i];
                        break;
                    case "--out":
                        outPath = args[++i];
                        break;
                    case "-h":
                    case "--help":
                        PrintUsage();
                        return 0;
                }
            }

            if (string.IsNullOrEmpty(payloadPath) || string.IsNullOrEmpty(outPath))
            {
                Console.Error.WriteLine("error: --payload and --out are both required");
                PrintUsage();
                return 2;
            }

            if (!File.Exists(payloadPath))
            {
                Console.Error.WriteLine($"error: payload not found at {payloadPath}");
                return 2;
            }

            try
            {
                WriteConfig(outPath);
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"error: failed to write injector config: {ex.Message}");
                return 1;
            }

            Process target;
            try
            {
                target = FindTargetProcess();
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"error: {ex.Message}");
                return 1;
            }

            Console.WriteLine($"target: {target.ProcessName} (pid {target.Id})");
            Console.WriteLine($"payload: {payloadPath}");
            Console.WriteLine($"out: {outPath}");

            byte[] bytes = File.ReadAllBytes(payloadPath);

            try
            {
                using (var injector = new Injector(target.Id))
                {
                    Console.WriteLine($"arch: {(injector.Is64Bit ? "x64" : "x86")}");
                    var assembly = injector.Inject(bytes, PayloadNamespace, PayloadClass, PayloadMethod);
                    Console.WriteLine($"injected: assembly handle 0x{assembly.ToInt64():X}");
                }
            }
            catch (InjectorException ex)
            {
                Console.Error.WriteLine($"injector failed: {ex.Message}");
                if (ex.InnerException != null)
                    Console.Error.WriteLine($"  inner: {ex.InnerException.Message}");
                return 1;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine($"unexpected error: {ex}");
                return 1;
            }

            return 0;
        }

        private static void WriteConfig(string outPath)
        {
            var dir = Path.Combine(Path.GetTempPath(), ConfigDir);
            Directory.CreateDirectory(dir);
            var configPath = Path.Combine(dir, ConfigFile);
            var json = JsonConvert.SerializeObject(new { out_path = outPath });
            File.WriteAllText(configPath, json);
        }

        private static Process FindTargetProcess()
        {
            // Wine exposes process names to .NET via NtQuerySystemInformation; the
            // shape ("MTGA", "MTGA.exe", path-prefixed) varies between Proton/wine
            // versions, so match on a substring rather than equality. Always log
            // the full process list to stderr first so a mismatch is diagnosable.
            var all = Process.GetProcesses();
            Console.Error.WriteLine($"debug: enumerating {all.Length} processes:");
            foreach (var p in all)
            {
                string name;
                try { name = p.ProcessName; }
                catch (Exception ex) { name = $"<error: {ex.Message}>"; }
                Console.Error.WriteLine($"  pid={p.Id} name='{name}'");
            }

            var matches = all
                .Where(p => SafeProcessNameContains(p, TargetProcess))
                .ToArray();

            if (matches.Length == 0)
                throw new Exception(
                    $"no running process matching '{TargetProcess}'. " +
                    "Launch MTGA and sign in to the main menu before running this.");

            if (matches.Length > 1)
                throw new Exception(
                    $"multiple '{TargetProcess}' processes found ({string.Join(", ", matches.Select(p => p.Id))}); " +
                    "kill the duplicates and retry.");

            return matches[0];
        }

        private static bool SafeProcessNameContains(Process p, string needle)
        {
            try
            {
                return p.ProcessName?.IndexOf(needle, StringComparison.OrdinalIgnoreCase) >= 0;
            }
            catch
            {
                return false;
            }
        }

        private static void PrintUsage()
        {
            Console.WriteLine("usage: mtg-inject --payload <MtgInventoryPayload.dll> --out <collection.json>");
            Console.WriteLine();
            Console.WriteLine("Injects the payload into the running MTGA.exe process and invokes");
            Console.WriteLine("MtgInventoryPayload.Loader.Load(). The payload polls until");
            Console.WriteLine("WrapperController.InventoryManager.Cards is populated, then writes");
            Console.WriteLine("the dictionary as JSON to <out>. Errors are written to <out>.err.");
        }
    }
}
