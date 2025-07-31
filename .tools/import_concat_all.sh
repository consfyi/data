#!/bin/bash
script_dir="$(dirname -- "${BASH_SOURCE[0]:-$0}")"
concat_importer="$script_dir/concat_importer.py"

$concat_importer anthrocon.json https://reg.anthrocon.org
$concat_importer las-vegas-fur-con.json https://reg.lasvegasfurcon.org
$concat_importer megaplex.json https://reg.megaplexcon.org
$concat_importer further-confusion.json https://reg.furtherconfusion.org
$concat_importer biggest-little-fur-con.json https://reg.goblfc.org
$concat_importer pawcon.json https://reg.pacanthro.org
