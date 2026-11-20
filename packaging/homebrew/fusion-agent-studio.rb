# Fusion-MLX Agent Studio — local agent development platform for Apple Silicon.
# Generated from PyPI 0.18.12 dependency resolution by gen_formula.py;
# regenerate on version bumps (see packaging/homebrew/README.md).
class FusionAgentStudio < Formula
  include Language::Python::Virtualenv

  desc "Local-first AI agent orchestration platform (daemon RPC + REST + CLI)"
  homepage "https://github.com/dahai80/fusion-agent-studio"
  url "https://files.pythonhosted.org/packages/f1/bf/cb9ef6a05bc928da8068fafaea5cc87b47f0c0720bb56c62b0309a712f77/fusion_agent_studio-0.18.12.tar.gz"
  sha256 "9113aab32520087f7a90de355d5698e1f97920653b072a9fb57d4ed5794b24ea"
  license "Apache-2.0"

  depends_on "python@3.12"

  resource "agent-client-protocol" do
    url "https://files.pythonhosted.org/packages/30/a1/7e29d00f01fede44ffdc84099552720727f8bfda942fea79a4f7d5ef1294/agent_client_protocol-1.0.0-py3-none-any.whl"
    sha256 "179c14266997841438ee15245a2668d31b2c695c13bff87d01c36e016185ddd0"
  end
  resource "aiofiles" do
    url "https://files.pythonhosted.org/packages/bc/8a/340a1555ae33d7354dbca4faa54948d76d89a27ceef032c8c3bc661d003e/aiofiles-25.1.0-py3-none-any.whl"
    sha256 "abe311e527c862958650f9438e859c1fa7568a141b22abcd015e120e86a85695"
  end
  resource "aiohappyeyeballs" do
    url "https://files.pythonhosted.org/packages/71/43/1947f06babed6b3f1d7f38b0c767f52df66bfb2bc10b468c4a7de9eceff2/aiohappyeyeballs-2.7.1-py3-none-any.whl"
    sha256 "9243213661e29250eb41368e5daa826fc017156c3b8a11440826b2e3ed376472"
  end
  resource "aiohttp" do
    url "https://files.pythonhosted.org/packages/68/30/173960c42b05a6c59f7558e4b12a4b0d9ba376cf6aa9bde7f9e08a30ca8d/aiohttp-3.14.5-py3-none-any.whl"
    sha256 "efc21a454892828368b11c2c780de0ff8bc991f73f6b99c6b66e56205470929b"
  end
  resource "aiosignal" do
    url "https://files.pythonhosted.org/packages/fb/76/641ae371508676492379f16e2fa48f4e2c11741bd63c48be4b12a6b09cba/aiosignal-1.4.0-py3-none-any.whl"
    sha256 "053243f8b92b990551949e63930a839ff0cf0b0ebbe0597b0f3fb19e1a0fe82e"
  end
  resource "annotated-doc" do
    url "https://files.pythonhosted.org/packages/5e/b1/9530d6bee606979d54501adcc767d958a38929827f11a524b0d93429d2ad/annotated_doc-0.0.7-py3-none-any.whl"
    sha256 "6bc31bbbec6dfaaedc1676dbc11dbda96fe22a2b5c400e9ebad113fec44a9dc6"
  end
  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/99/91/8acff4f5e50511b911bbccb72b8628a49c68ce14148cd9f6431094859a90/annotated_types-0.8.0-py3-none-any.whl"
    sha256 "f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0"
  end
  resource "anyio" do
    url "https://files.pythonhosted.org/packages/a9/16/7757fb84c819accc7d7e9af5291b261e4abb8627ee040f99e7d35ef2457a/anyio-4.16.0-py3-none-any.whl"
    sha256 "c66ae634e0b8228396b801741beb3294326d4b6102f7bba4041c0dfb58c29026"
  end
  resource "attrs" do
    url "https://files.pythonhosted.org/packages/64/b4/17d4b0b2a2dc85a6df63d1157e028ed19f90d4cd97c36717afef2bc2f395/attrs-26.1.0-py3-none-any.whl"
    sha256 "c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309"
  end
  resource "certifi" do
    url "https://files.pythonhosted.org/packages/32/d1/a3652fe2c19134d0d15922177821d863cbd7e842d53a21bbe7e8e3a1b5ac/certifi-2026.10.23-py3-none-any.whl"
    sha256 "e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1"
  end
  resource "click" do
    url "https://files.pythonhosted.org/packages/31/7d/164b7ffc8624fe9efb4221bf9af485d5c1e41b9ae118045d2b6dc5457d8b/click-8.5.1-py3-none-any.whl"
    sha256 "8bb5376d2006950386f8c6427613334a2acadc5f559cb80e638d00b4df957e37"
  end
  resource "fastapi" do
    url "https://files.pythonhosted.org/packages/c4/c1/3b74c030262a1b38b1fee180f80b4007bbc11c9ac12028cb18c7bcecaec9/fastapi-0.165.0-py3-none-any.whl"
    sha256 "896f48462042bb4f162147eb8fac7e858753dc1f486765709baf105d70e4fc5b"
  end
  resource "frozenlist" do
    url "https://files.pythonhosted.org/packages/9a/9a/e35b4a917281c0b8419d4207f4334c8e8c5dbf4f3f5f9ada73958d937dcc/frozenlist-1.8.0-py3-none-any.whl"
    sha256 "0c18a16eab41e82c295618a77502e17b195883241c563b00f0aa5106fc4eaa0d"
  end
  resource "fusion-core" do
    url "https://files.pythonhosted.org/packages/8f/a2/bc0151fc363e7fb7dc0f36cce54b08e15f3e333aa4977706bb180be1c6ce/fusion_core-0.15.0-py3-none-any.whl"
    sha256 "1bc481ee218b009aaacde8c7dda57a40e33050efc84acf6cf9df0ba07c9bb33f"
  end
  resource "fusion-harness" do
    url "https://files.pythonhosted.org/packages/97/78/2be0222eb705ebb56a99581f68a08471da241b66206fa7b00b60def26438/fusion_harness-0.27.0-py3-none-any.whl"
    sha256 "adb6c97d903166f5822fdbbc86737bb8952cbf447ffe0bec0712349a286cbfeb"
  end
  resource "h11" do
    url "https://files.pythonhosted.org/packages/04/4b/29cac41a4d98d144bf5f6d33995617b185d14b22401f75ca86f384e87ff1/h11-0.16.0-py3-none-any.whl"
    sha256 "63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86"
  end
  resource "httpcore" do
    url "https://files.pythonhosted.org/packages/7e/f5/f66802a942d491edb555dd61e3a9961140fd64c90bce1eafd741609d334d/httpcore-1.0.9-py3-none-any.whl"
    sha256 "2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55"
  end
  resource "httpx" do
    url "https://files.pythonhosted.org/packages/2a/39/e50c7c3a983047577ee07d2a9e53faf5a69493943ec3f6a384bdc792deb2/httpx-0.28.1-py3-none-any.whl"
    sha256 "d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad"
  end
  resource "idna" do
    url "https://files.pythonhosted.org/packages/58/a2/bb081bab032533a855d44de1d56f8e8426114ff1ba5d1f07a438a0a654f8/idna-3.20-py3-none-any.whl"
    sha256 "ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c"
  end
  resource "multidict" do
    url "https://files.pythonhosted.org/packages/9d/96/1aaad0e5df33f1f5af21775c411349c0234a6edd581ea403bc81919a6eab/multidict-7.2.1-py3-none-any.whl"
    sha256 "08ea94663d9e7c25d14d78222f7aee906c472d4837766bec62477c38f6e419dc"
  end
  resource "opentelemetry-api" do
    url "https://files.pythonhosted.org/packages/a9/2d/610db864d7513158cebb8f36cc88e67e73bd6c7aa1905ef18d89e1281c81/opentelemetry_api-1.45.2-py3-none-any.whl"
    sha256 "2cbd737377e710ee740d261c94e93fa0f08e6d037c18f17513cc0a2d94479a34"
  end
  resource "propcache" do
    url "https://files.pythonhosted.org/packages/f5/cd/785c64ed382f3f04201870267b02783f63b4678c2acfddc177a3ebcc2727/propcache-0.5.4-py3-none-any.whl"
    sha256 "62c60aec739ed00124573cce1178138fd690c7676352d67a37328c1cf51d7468"
  end
  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/8a/8c/824a686c4ba815ddb0722f9e66272088bccd4e1ae1d2f7015375e4a4fc7f/pydantic-2.14.4-py3-none-any.whl"
    sha256 "653a2ad9f6d8858529c77c72717d0ea991d6c6837fe683246b2f4b2bbfc3fadc"
  end
  resource "setproctitle" do
    url "https://files.pythonhosted.org/packages/b1/36/e7a4b93d7457ed12a1a2b49b042c25848a8847b15f7808159f2ccce91fdd/setproctitle-1.3.8-cp312-cp312-macosx_10_13_universal2.whl"
    sha256 "f9a49d8103df362272c625641a4f7b104ae584db21d0f1eec3b7a5d0d7750ed8"
  end
  resource "starlette" do
    url "https://files.pythonhosted.org/packages/88/2e/947cfe02def9371c07cd5bf5b6b61c829e9aecb9904db02a66cfafbc1eec/starlette-1.10.0-py3-none-any.whl"
    sha256 "478c0ab3b4ced6e82719849c118fa406f0278a3499c1a898cd15cd31bdc38ce4"
  end
  resource "typing_extensions" do
    url "https://files.pythonhosted.org/packages/49/d3/b8441a820a491ddfc024b0b0cf0393375b75ea13866d9c66727e54c2fc80/typing_extensions-4.16.0-py3-none-any.whl"
    sha256 "481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8"
  end
  resource "typing-inspection" do
    url "https://files.pythonhosted.org/packages/de/0c/ea2544850a7c135e15033a5c7a8e7211136184b7ae63e2239d557c2b3019/typing_inspection-0.4.5-py3-none-any.whl"
    sha256 "bc210535442b39e1bf2805e0c7a17dfd6a4ace760c84c7f7d16245d23a6b1c39"
  end
  resource "uvicorn" do
    url "https://files.pythonhosted.org/packages/38/0c/b54a4fdd7f90a3af8b02ebc9ce6712c2c208b7926a2f7bad95c33ebbe943/uvicorn-0.54.0-py3-none-any.whl"
    sha256 "505bdb0f318731d45f1f712071fc781a8981f6847a31c902c9f5e652d4f67faf"
  end
  resource "yarl" do
    url "https://files.pythonhosted.org/packages/54/22/318c7980066769c6bcd9221ed2248294f5698811da099013098c670565ed/yarl-1.25.1-py3-none-any.whl"
    sha256 "681c758b0490f9e96b78e5fa8e8dc6e648e9185bb6eaebe73183c33ea0c445f3"
  end
  on_macos do
    on_arm do
      resource "psutil" do
        url "https://files.pythonhosted.org/packages/80/c4/f5af4c1ca8c1eeb2e92ccca14ce8effdeec651d5ab6053c589b074eda6e1/psutil-7.2.2-cp36-abi3-macosx_11_0_arm64.whl"
        sha256 "1a7b04c10f32cc88ab39cbf606e117fd74721c831c98a27dc04578deb0c16979"
      end
      resource "pydantic_core" do
        url "https://files.pythonhosted.org/packages/9f/6f/4e172894c2a4ca3bacd6c5f823ea5576df25eafe7593ffeb3576895b92aa/pydantic_core-2.50.5-cp312-cp312-macosx_11_0_arm64.whl"
        sha256 "a9bcf23628860d5a90cacdc188ee3b7d0547a38eb985214f03e7002a6ca0e558"
      end
      resource "pyyaml" do
        url "https://files.pythonhosted.org/packages/89/a0/6cf41a19a1f2f3feab0e9c0b74134aa2ce6849093d5517a0c550fe37a648/pyyaml-6.0.3-cp312-cp312-macosx_11_0_arm64.whl"
        sha256 "fc09d0aa354569bc501d4e787133afc08552722d3ab34836a80547331bb5d4a0"
      end
    end
    on_intel do
      resource "psutil" do
        url "https://files.pythonhosted.org/packages/e7/36/5ee6e05c9bd427237b11b3937ad82bb8ad2752d72c6969314590dd0c2f6e/psutil-7.2.2-cp36-abi3-macosx_10_9_x86_64.whl"
        sha256 "ed0cace939114f62738d808fdcecd4c869222507e266e574799e9c0faa17d486"
      end
      resource "pydantic_core" do
        url "https://files.pythonhosted.org/packages/2e/ce/c79f87db419571137da902afa5cb16f7a981943bb9e0a520886fc4ed06e6/pydantic_core-2.50.5-cp312-cp312-macosx_10_12_x86_64.whl"
        sha256 "24d23de96195ff9637e5940a7fbde3bea6a355b26fb5f491737dd803aa5d5a93"
      end
      resource "pyyaml" do
        url "https://files.pythonhosted.org/packages/d1/33/422b98d2195232ca1826284a76852ad5a86fe23e31b009c9886b2d0fb8b2/pyyaml-6.0.3-cp312-cp312-macosx_10_13_x86_64.whl"
        sha256 "7f047e29dcae44602496db43be01ad42fc6f1cc0d8cd6c83d342306c32270196"
      end
    end
  end

  # virtualenv_install_with_resources only auto-detects *-py3-none-any.whl
  # resources (language/python.rb matches "[.-]py3[^-]*-none-any.whl$");
  # platform-specific wheels (cp312/abi3) would be unpacked and handed to pip
  # as a directory, which fails. Exclude them and pip-install the wheel files
  # directly (fetch still verifies sha256).
  def install
    platform_wheels = %w[setproctitle pydantic_core pyyaml psutil]
    virtualenv_install_with_resources(without: platform_wheels)
    pip = Formula["python@3.12"].opt_bin/"python3.12"
    platform_wheels.each do |name|
      res = resource(name)
      # stage triggers the download + sha256 verification; pip still gets the
      # cached wheel file itself (calling res.fetch here trips a brew bug:
      # NameError: uninitialized constant CurlDownloadStrategy::Timeout).
      res.stage do
        # pip rejects Homebrew's hash-prefixed cache filename as an invalid
        # wheel filename; copy into the stage under the clean PyPI basename.
        whl = Pathname.pwd / File.basename(res.url)
        FileUtils.cp res.cached_download, whl
        system pip, "-m", "pip", "--python=#{libexec}/bin/python",
          "install", "--no-deps", "--no-compile", "--ignore-installed", whl
      end
    end
  end

  test do
    output = shell_output("#{bin}/fusion-agent --help")
    assert_match "submit", output
  end
end
