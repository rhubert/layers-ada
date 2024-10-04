ASL Plugin
==========

The AdaLanguageServer (ALS) provides an implementation of the Language-Server-
Protocol (LSP) for Ada. For the LSP there are several plugins for nearly all
common editors available. When $editor is connected to ALS it can support:

 - Code completion for names, keywords, aggregates, ...
 - Code navigation, such as Go to Definition/Declaration, ...
 - Code refactoring like insert named associations, auto-add with-clauses, ...
 - Document/Workspace symbol search.
 - Code folding and formatting.

The feature set depends on the used editor / plugin.


A minimal ALS configuration needs a default-gpr file and a GPR_PROJECT_PATH to find
all dependencies. To be able to parse the gpr files all `external` used
environment variables must exists. Otherwise an error is issued and the ALS
refuses to work.

In a Bob-workspace there are several challenges:
 - not all sources are available
 - available sources are located in 'unknown' folders
 - many unrelated gpr files
 - environment variables are often set in the buildScript before calling
   gprBuild

To make ALS work in a bob-env we need to:
 1) Build the project(s) and checkout (at least all) sources we want to edit
 2) collect relevant GPR_PROJECT_PATH
 3) provide all needed `external` referenced environment
 4) provide editor specific configuration

Build the project(s)
====================

This works as usual:

```
 bob dev <myproject>
```

This will checkout `myproject`'s sources and potentially download all deps.
While this is working with the ALS-plugin all dependency sources point to their
dist-folders where they shouldn't be edited. To get a good starting point for
sources.

```
 bob dev <myproject> --always-checkout <pattern>
```

can be used to checkout the sources for all packages matching a pattern.

Generate Editor configuration
=============================

ATM the ALS plugin provides generators for NeoVim, VisualStudioCode and Emacs.

Run the generator:
```
bob project -n <generatorName> <package-query> --name <name>
```

With `generatorName`:

| Editor     | GeneratorName |
|------------|---------------|
| NeoVim     | als_nvim      |
| Vim        | als_vim       |
| VSCode     | als_vscode    |
| Emacs      | als_emacs     |
| GnatStudio | gnatstudio    |

`package-query` is just a usual bob-path. (e.g. same as used to build).
`name` is the name of the generated project. By default the package-path will be
used.

Note: the plugin will fail if the query matches packages with different RTS.

Other Options:

`-D Key=Value` used to add environment variables
`-i name` ignore directories
`--gpr file` Default gpr file. By default all gpr files in the package-root will
be used.
`-S <package>`. Use source workspace for `<package>`. `<package>` used as regex pattern.
`-K <package>`. Use dist workspace for `<package>`. `<package>` used as regex pattern.

Environment handling
--------------------

The plugin uses the `buildSetup` script to collect the build environment. This
should work without `-S` out of the box. If dependencies should be visible as
their source packages this will also work as long as no environment variables
with different values are detected. In this case the plugin will issue a warning
and the result might be unusable.

Exclude Directories
-------------------

Exclude (`-i`) can also be used to exclude a subdirectory of the root-project in order
to avoid picking up gpr files from there.

Editor specific configurations / setup
======================================

GnatStudio
----------

If the project should be compiled with gnatStudio some additional arguments must be added to
`gprbuild`. These can be found in the generated `additional_buildargs.txt` next to the startscript.

Visual Studio Code
------------------

In VSCode the AdaLanguageServer extension needs to be installed.

NeoVim
------

NeoVim comes with a built-in lsp-client so no further plugin in needed.

The completion popup can be remapped to Shift-Tab using
```
inoremap <S-TAB> <C-X><C-O>
```
in the `init.vim`.

Vim
---

For vim the vim-lsp module needs to be loaded. Add something like the following to .vimrc:

```
set runtimepath^=~/.vim/bundle/vim-lsp
set runtimepath^=~/.vim/bundle/vim-lsp-settings

function! s:on_lsp_buffer_enabled() abort
    setlocal omnifunc=lsp#complete
    setlocal signcolumn=yes
    if exists('+tagfunc') | setlocal tagfunc=lsp#tagfunc | endif
    nmap <buffer> gd <plug>(lsp-definition)
    nmap <buffer> gs <plug>(lsp-document-symbol-search)
    nmap <buffer> gS <plug>(lsp-workspace-symbol-search)
    nmap <buffer> gr <plug>(lsp-references)
    nmap <buffer> gi <plug>(lsp-implementation)
    nmap <buffer> gt <plug>(lsp-type-definition)
    nmap <buffer> <leader>rn <plug>(lsp-rename)
    nmap <buffer> [g <plug>(lsp-previous-diagnostic)
    nmap <buffer> ]g <plug>(lsp-next-diagnostic)
    nmap <buffer> K <plug>(lsp-hover)
    nnoremap <buffer> <expr><c-f> lsp#scroll(+4)
    nnoremap <buffer> <expr><c-d> lsp#scroll(-4)

    let g:lsp_format_sync_timeout = 1000
    autocmd! BufWritePre *.rs,*.go call execute('LspDocumentFormatSync')

    " refer to doc to add more commands
endfunction

augroup lsp_install
    au!
    " call s:on_lsp_buffer_enabled only for languages that has the server registered.
    autocmd User lsp_buffer_enabled call s:on_lsp_buffer_enabled()
augroup END

set runtimepath^=~/.vim/bundle/asyncomplete.vim
set runtimepath^=~/.vim/bundle/asyncomplete-lsp.vim
inoremap <expr> <Tab>   pumvisible() ? "\<C-n>" : "\<Tab>"
inoremap <expr> <S-Tab> pumvisible() ? "\<C-p>" : "\<S-Tab>"
inoremap <expr> <cr>    pumvisible() ? asyncomplete#close_popup() : "\<cr>"
```

Emacs
-----

Emacs needs many packages to be able to use lsp. A possible way to get them into
devnet is to use a VM with internet access, install them using `package-install`
and transfer them to devnet.

This is a (potentially incomplete) list of packages:

```
dash
f
s
ht
hydra
markdown_mode
spinner
lsp-mode
lsp-mode/clients
use-package

ada-mode-8.0.5
eglot-1.15
eldoc-1.14.0
external-completion-0.1
flymake-1.3.4
gnat-compiler-1.0.2
jsonrpc-1.0.17
project-0.9.8
seq-2.23
uniquify-files-1.0.4
wisi-4.2.2
xref-1.6.3
lsp-ui-8.0.1
company-mode
```

Example
=======

To generate a gnatStudio configuration for `mlp::gcore` use:

```
bob project -n gnatstudio \ # start bob project with 'gnatstudio' generator
    //mlp::mlp-gcore \      # package-query
    --name gcore \          # name of the project
    -S mlp* \               # all packages matching `mlp*` are used as source packages
    -K .*gcore-audit* \     # use dist of all packages matching `.*gcore-audit*`
    -K mlp::libaudit-events-common-dev \    # use dist of mlp::libaudit-events-common-dev
    -K mlp::liblsksupport-dev               # use dist of mlp::liblsksupport-dev
```
