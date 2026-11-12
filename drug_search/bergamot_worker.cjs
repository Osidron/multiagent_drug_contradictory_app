const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

const assets = path.resolve(process.argv[2]);
const source = JSON.parse(fs.readFileSync(0, "utf8"));
const read = (name) => fs.readFileSync(path.join(assets, name));

// The pinned Mozilla Emscripten script reads Module in its own scope.
const glue = read("bergamot-translator-worker.js").toString("utf8");
const load = new Function("Module", "require", "__dirname", "__filename", "console", glue);
const diagnostics = Object.assign(Object.create(console), { log: console.error });
const engine = {
  wasmBinary: read("bergamot-translator-worker.wasm"),
  print: (message) => console.error(message),
  onRuntimeInitialized: translate,
};
load(engine, require, assets, path.join(assets, "bergamot-translator-worker.js"), diagnostics);

function alignedFile(name, alignment) {
  const bytes = zlib.gunzipSync(read(name));
  const memory = new engine.AlignedMemory(bytes.length, alignment);
  memory.getByteArrayView().set(bytes);
  return memory;
}

function translate() {
  const modelFile = alignedFile("model.ruen.intgemm.alphas.bin.gz", 256);
  const shortlist = alignedFile("lex.50.50.ruen.s2t.bin.gz", 64);
  const vocab = new engine.AlignedMemoryList();
  vocab.push_back(alignedFile("vocab.ruen.spm.gz", 64));
  const config = [
    "beam-size: 1", "normalize: 1.0", "word-penalty: 0", "alignment: soft",
    "max-length-break: 128", "mini-batch-words: 1024", "workspace: 64",
    "max-length-factor: 2.0", "skip-cost: true", "cpu-threads: 0",
    "quiet: true", "quiet-translation: true", "gemm-precision: int8shiftAlphaAll",
  ].join("\n");
  const model = new engine.TranslationModel(config, modelFile, shortlist, vocab, null);
  const service = new engine.BlockingService({ cacheSize: 0 });
  const input = new engine.VectorString();
  input.push_back(source);
  const options = new engine.VectorResponseOptions();
  options.push_back({ qualityScores: false, alignment: false, html: false });
  const result = service.translate(model, input, options);
  process.stdout.write(JSON.stringify(result.get(0).getTranslatedText()));
  // This worker handles one request. Its WebAssembly memory is reclaimed on exit.
}
