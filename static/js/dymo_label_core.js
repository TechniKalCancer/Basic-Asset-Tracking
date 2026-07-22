// Shared Dymo label template + builder for asset tag / assigned-person labels.
// Used by both the single-asset Print Label card and the bulk print page.
// Label stock: 30252 Address (1-1/8" x 3-1/2"). Geometry (PaperName, DrawCommands,
// outer Bounds) reused verbatim from DYMO's own official sample for this stock, so
// the printable area is known-safe. Layout: AssetTag/PersonName text on the left
// half, a Code 128 barcode of the asset tag on the right half.
var DYMO_LABEL_XML = '<?xml version="1.0" encoding="utf-8"?>' +
    '<DieCutLabel Version="8.0" Units="twips">' +
    '<PaperOrientation>Landscape</PaperOrientation>' +
    '<Id>Address</Id>' +
    '<PaperName>30252 Address</PaperName>' +
    '<DrawCommands>' +
    '<RoundRectangle X="0" Y="0" Width="1581" Height="5040" Rx="270" Ry="270" />' +
    '</DrawCommands>' +
    '<ObjectInfo>' +
    '<TextObject>' +
    '<Name>AssetTag</Name>' +
    '<ForeColor Alpha="255" Red="0" Green="0" Blue="0" />' +
    '<BackColor Alpha="0" Red="255" Green="255" Blue="255" />' +
    '<LinkedObjectName></LinkedObjectName>' +
    '<Rotation>Rotation0</Rotation>' +
    '<IsMirrored>False</IsMirrored>' +
    '<IsVariable>True</IsVariable>' +
    '<HorizontalAlignment>Left</HorizontalAlignment>' +
    '<VerticalAlignment>Middle</VerticalAlignment>' +
    '<TextFitMode>ShrinkToFit</TextFitMode>' +
    '<UseFullFontHeight>True</UseFullFontHeight>' +
    '<Verticalized>False</Verticalized>' +
    '<StyledText>' +
    '<Element>' +
    '<String>Asset Tag</String>' +
    '<Attributes>' +
    '<Font Family="Arial" Size="18" Bold="True" Italic="False" Underline="False" Strikeout="False" />' +
    '<ForeColor Alpha="255" Red="0" Green="0" Blue="0" />' +
    '</Attributes>' +
    '</Element>' +
    '</StyledText>' +
    '</TextObject>' +
    '<Bounds X="332" Y="150" Width="2200" Height="600" />' +
    '</ObjectInfo>' +
    '<ObjectInfo>' +
    '<TextObject>' +
    '<Name>PersonName</Name>' +
    '<ForeColor Alpha="255" Red="0" Green="0" Blue="0" />' +
    '<BackColor Alpha="0" Red="255" Green="255" Blue="255" />' +
    '<LinkedObjectName></LinkedObjectName>' +
    '<Rotation>Rotation0</Rotation>' +
    '<IsMirrored>False</IsMirrored>' +
    '<IsVariable>True</IsVariable>' +
    '<HorizontalAlignment>Left</HorizontalAlignment>' +
    '<VerticalAlignment>Middle</VerticalAlignment>' +
    '<TextFitMode>ShrinkToFit</TextFitMode>' +
    '<UseFullFontHeight>True</UseFullFontHeight>' +
    '<Verticalized>False</Verticalized>' +
    '<StyledText>' +
    '<Element>' +
    '<String>Assigned To</String>' +
    '<Attributes>' +
    '<Font Family="Arial" Size="12" Bold="False" Italic="False" Underline="False" Strikeout="False" />' +
    '<ForeColor Alpha="255" Red="0" Green="0" Blue="0" />' +
    '</Attributes>' +
    '</Element>' +
    '</StyledText>' +
    '</TextObject>' +
    '<Bounds X="332" Y="780" Width="2200" Height="600" />' +
    '</ObjectInfo>' +
    '<ObjectInfo>' +
    '<BarcodeObject>' +
    '<Name>Barcode</Name>' +
    '<ForeColor Alpha="255" Red="0" Green="0" Blue="0" />' +
    '<BackColor Alpha="255" Red="255" Green="255" Blue="255" />' +
    '<LinkedObjectName></LinkedObjectName>' +
    '<Rotation>Rotation0</Rotation>' +
    '<IsMirrored>False</IsMirrored>' +
    '<IsVariable>True</IsVariable>' +
    '<Text>000000</Text>' +
    '<Type>Code128Auto</Type>' +
    '<Size>Small</Size>' +
    '<TextPosition>None</TextPosition>' +
    '<TextFont Family="Arial" Size="8" Bold="False" Italic="False" Underline="False" Strikeout="False" />' +
    '<CheckSumFont Family="Arial" Size="8" Bold="False" Italic="False" Underline="False" Strikeout="False" />' +
    '<TextEmbedding>None</TextEmbedding>' +
    '<ECLevel>0</ECLevel>' +
    '<HorizontalAlignment>Center</HorizontalAlignment>' +
    '<QuietZonesPadding Left="0" Top="0" Right="0" Bottom="0" />' +
    '</BarcodeObject>' +
    '<Bounds X="2700" Y="415" Width="2200" Height="700" />' +
    '</ObjectInfo>' +
    '</DieCutLabel>';

/**
 * Builds a ready-to-print/preview Dymo label object for one asset, with a
 * Code 128 barcode of the (unmodified) asset tag to the right of the text.
 * @param {string} assetTag
 * @param {string} personName - empty string if unassigned
 * @param {boolean} isCharger - true to mark this as the charger's label
 * @returns {*} a DYMO label object (has .render(), .print(printerName))
 */
function buildAssetLabel(assetTag, personName, isCharger) {
    var label = dymo.label.framework.openLabelXml(DYMO_LABEL_XML);
    label.setObjectText('AssetTag', assetTag + (isCharger ? ' — CHARGER' : ''));
    label.setObjectText('PersonName', personName ? 'Assigned to: ' + personName : 'Unassigned');
    try {
        // Barcode always encodes the plain asset tag (not the "— CHARGER" suffix)
        // so scanning either label — device or charger — resolves the same asset.
        label.setObjectText('Barcode', assetTag);
    } catch (e) {
        // If this DYMO Connect version rejects setObjectText on a barcode object,
        // fail soft — the label still prints fine with the text fields alone.
    }
    return label;
}
