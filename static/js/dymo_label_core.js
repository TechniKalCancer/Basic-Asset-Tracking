// Shared Dymo label template + builder for asset tag / assigned-person labels.
// Used by both the single-asset Print Label card and the bulk print page.
// Label stock: 30252 Address (1-1/8" x 3-1/2"). Geometry (PaperName, DrawCommands,
// outer Bounds) reused verbatim from DYMO's own official sample for this stock, so
// the printable area is known-safe; only the two TextObjects are custom.
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
    '<Bounds X="332" Y="150" Width="4455" Height="600" />' +
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
    '<Bounds X="332" Y="780" Width="4455" Height="600" />' +
    '</ObjectInfo>' +
    '</DieCutLabel>';

/**
 * Builds a ready-to-print/preview Dymo label object for one asset.
 * @param {string} assetTag
 * @param {string} personName - empty string if unassigned
 * @param {boolean} isCharger - true to mark this as the charger's label
 * @returns {*} a DYMO label object (has .render(), .print(printerName))
 */
function buildAssetLabel(assetTag, personName, isCharger) {
    var label = dymo.label.framework.openLabelXml(DYMO_LABEL_XML);
    label.setObjectText('AssetTag', assetTag + (isCharger ? ' — CHARGER' : ''));
    label.setObjectText('PersonName', personName ? 'Assigned to: ' + personName : 'Unassigned');
    return label;
}
